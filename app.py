from __future__ import annotations

import csv
import io
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads"
BACKUP_DIR = BASE_DIR / "backups"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))

database_url = os.environ.get("DATABASE_URL")
if database_url:
    database_url = database_url.replace("postgres://", "postgresql://", 1)
else:
    database_url = f"sqlite:///{BASE_DIR / 'licitacoes.db'}"

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


STATUS_OPTIONS = ["em andamento", "finalizada", "ganha", "perdida"]
ITEM_RESULT_OPTIONS = ["em disputa", "ganho", "perdido"]
WON_FOLLOWUP_OPTIONS = ["Em habilitação", "Em adjudicação", "Homologado"]


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Tender(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    edital_number = db.Column(db.String(80), nullable=False)
    dispute_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(30), default="em andamento", nullable=False)
    won_followup_status = db.Column(db.String(30), default="", nullable=False)
    won_notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    notes = db.Column(db.Text, default="")

    items = db.relationship("Item", backref="tender", lazy=True, cascade="all, delete-orphan")
    attachments = db.relationship("Attachment", backref="tender", lazy=True, cascade="all, delete-orphan")

    @property
    def won_status_badge(self) -> str:
        mapping = {
            "Em habilitação": "warning",
            "Em adjudicação": "primary",
            "Homologado": "success",
        }
        return mapping.get(self.won_followup_status or "", "secondary")

    @property
    def won_items_count(self) -> int:
        return sum(1 for item in self.items if item.result_status == "ganho")


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    product_link = db.Column(db.String(500), default="")
    price_found = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    items = db.relationship("Item", backref="supplier", lazy=True)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tender_id = db.Column(db.Integer, db.ForeignKey("tender.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    item_number = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False)
    quantity = db.Column(db.Float, default=1, nullable=False)
    reference_value = db.Column(db.Float, default=0, nullable=False)
    cost_value = db.Column(db.Float, default=0, nullable=False)
    min_margin_value = db.Column(db.Float, default=0, nullable=False)
    alert_percent = db.Column(db.Float, default=5, nullable=False)
    last_bid = db.Column(db.Float, nullable=True)
    decrement_step = db.Column(db.Float, default=1, nullable=False)
    final_value = db.Column(db.Float, nullable=True)
    result_status = db.Column(db.String(20), default="em disputa", nullable=False)
    product_link = db.Column(db.String(500), default="")
    price_found = db.Column(db.Float, default=0)
    quick_notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    bids = db.relationship("BidHistory", backref="item", lazy=True, cascade="all, delete-orphan")

    @property
    def max_cost_allowed(self) -> float:
        return round((self.reference_value or 0) / 2, 4)

    @property
    def difference_to_limit(self) -> float:
        return round(self.max_cost_allowed - (self.price_found or 0), 4)

    @property
    def viability_label(self) -> str:
        if (self.price_found or 0) == 0 and (self.reference_value or 0) == 0:
            return "sem dados"
        if self.price_status == "success":
            return "viável"
        if self.price_status == "warning":
            return "no limite"
        return "não viável"

    @property
    def price_status(self) -> str:
        price = self.price_found or 0
        limit = self.max_cost_allowed
        if limit <= 0:
            return "secondary"
        if price <= limit * 0.9:
            return "success"
        if price <= limit:
            return "warning"
        return "danger"

    @property
    def quick_value(self) -> float:
        return self.max_cost_allowed

    @property
    def potential_profit(self) -> float:
        return round((self.reference_value or 0) - (self.price_found or 0), 4)

    @property
    def supplier_name(self) -> str:
        return self.supplier.name if self.supplier else "Não informado"


class BidHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    bid_value = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tender_id = db.Column(db.Integer, db.ForeignKey("tender.id"), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


def currency(value: float | None) -> str:
    value = float(value or 0)
    return f"R$ {value:,.4f}".replace(",", "X").replace(".", ",").replace("X", ".")


app.jinja_env.filters["currency"] = currency


@app.context_processor
def inject_globals():
    return {
        "today": date.today(),
        "status_options": STATUS_OPTIONS,
        "item_result_options": ITEM_RESULT_OPTIONS,
        "won_followup_options": WON_FOLLOWUP_OPTIONS,
    }


def ensure_schema() -> None:
    db.create_all()
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()

    if "tender" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("tender")}
    statements: list[str] = []

    if "won_followup_status" not in columns:
        statements.append("ALTER TABLE tender ADD COLUMN won_followup_status VARCHAR(30) NOT NULL DEFAULT ''")
    if "won_notes" not in columns:
        statements.append("ALTER TABLE tender ADD COLUMN won_notes TEXT DEFAULT ''")

    if statements:
        with db.engine.connect() as conn:
            for stmt in statements:
                conn.exec_driver_sql(stmt)
            conn.commit()


def seed_admin() -> None:
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@licitacao.local").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    admin_name = os.environ.get("ADMIN_NAME", "Administrador").strip()

    if not User.query.filter_by(email=admin_email).first():
        admin = User(name=admin_name, email=admin_email)
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()


def normalize_won_status(tender: Tender) -> None:
    if tender.status != "ganha":
        tender.won_followup_status = ""
    elif not tender.won_followup_status:
        tender.won_followup_status = "Em habilitação"


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("E-mail ou senha inválidos.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    status = request.args.get("status", "")
    query = Tender.query
    if status:
        query = query.filter(Tender.status == status)
    tenders = query.order_by(Tender.dispute_date.asc()).all()

    all_items = Item.query.order_by(Item.updated_at.desc()).all()
    total_tenders = Tender.query.count()
    total_items = len(all_items)
    viable_items = sum(1 for item in all_items if item.price_status == "success")
    risk_items = sum(1 for item in all_items if item.price_status == "danger")
    won_tenders_count = Tender.query.filter(Tender.status == "ganha").count()
    potential_profit = sum(item.potential_profit for item in all_items if item.price_found)
    recent_bids = BidHistory.query.order_by(BidHistory.created_at.desc()).limit(8).all()
    upcoming = Tender.query.filter(Tender.dispute_date >= date.today()).order_by(Tender.dispute_date.asc()).limit(5).all()
    quick_items = [item for item in all_items if item.reference_value or item.price_found][:8]

    return render_template(
        "dashboard.html",
        tenders=tenders,
        total_tenders=total_tenders,
        total_items=total_items,
        viable_items=viable_items,
        risk_items=risk_items,
        won_tenders_count=won_tenders_count,
        potential_profit=potential_profit,
        recent_bids=recent_bids,
        upcoming=upcoming,
        quick_items=quick_items,
        status=status,
    )


@app.route("/tenders", methods=["GET", "POST"])
@login_required
def tenders():
    if request.method == "POST":
        tender = Tender(
            edital_number=request.form.get("edital_number", "").strip(),
            dispute_date=datetime.strptime(request.form.get("dispute_date"), "%Y-%m-%d").date(),
            status=request.form.get("status", "em andamento"),
            notes=request.form.get("notes", "").strip(),
            won_notes=request.form.get("won_notes", "").strip(),
        )
        normalize_won_status(tender)
        db.session.add(tender)
        db.session.commit()
        flash("Licitação cadastrada com sucesso.", "success")
        return redirect(url_for("tender_detail", tender_id=tender.id))

    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip()
    query = Tender.query
    if search:
        query = query.filter(Tender.edital_number.ilike(f"%{search}%"))
    if status:
        query = query.filter(Tender.status == status)
    tenders_list = query.order_by(Tender.dispute_date.asc()).all()
    return render_template("tenders.html", tenders=tenders_list, search=search, status=status)


@app.route("/tenders/<int:tender_id>", methods=["GET", "POST"])
@login_required
def tender_detail(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    if request.method == "POST":
        item = Item(
            tender_id=tender.id,
            supplier_id=request.form.get("supplier_id") or None,
            item_number=request.form.get("item_number", "").strip(),
            description=request.form.get("description", "").strip(),
            quantity=float(request.form.get("quantity") or 1),
            reference_value=float(request.form.get("reference_value") or 0),
            price_found=float(request.form.get("price_found") or 0),
            product_link=request.form.get("product_link", "").strip(),
            quick_notes=request.form.get("quick_notes", "").strip(),
        )
        db.session.add(item)
        db.session.commit()
        flash("Item adicionado com sucesso.", "success")
        return redirect(url_for("tender_detail", tender_id=tender.id))

    search = request.args.get("search", "").strip()
    viability_filter = request.args.get("viability", "").strip()
    result_filter = request.args.get("result", "").strip()
    items_query = Item.query.filter_by(tender_id=tender.id)
    if search:
        items_query = items_query.filter(
            or_(Item.description.ilike(f"%{search}%"), Item.item_number.ilike(f"%{search}%"))
        )
    if result_filter:
        items_query = items_query.filter(Item.result_status == result_filter)
    items = items_query.order_by(Item.item_number.asc()).all()
    if viability_filter:
        items = [item for item in items if item.price_status == viability_filter]

    suppliers_list = Supplier.query.order_by(Supplier.name.asc()).all()
    selected_quick_id = request.args.get("quick_item_id", type=int)
    selected_quick_item = None
    if selected_quick_id:
        selected_quick_item = next((item for item in items if item.id == selected_quick_id), None)
    if not selected_quick_item and items:
        selected_quick_item = items[0]

    return render_template(
        "tender_detail.html",
        tender=tender,
        items=items,
        suppliers=suppliers_list,
        search=search,
        result_filter=result_filter,
        viability_filter=viability_filter,
        selected_quick_item=selected_quick_item,
    )


@app.route("/tenders/<int:tender_id>/update", methods=["POST"])
@login_required
def update_tender(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    tender.edital_number = request.form.get("edital_number", tender.edital_number).strip()
    tender.dispute_date = datetime.strptime(request.form.get("dispute_date"), "%Y-%m-%d").date()
    tender.status = request.form.get("status", tender.status)
    tender.notes = request.form.get("notes", tender.notes)
    tender.won_notes = request.form.get("won_notes", tender.won_notes)
    if tender.status == "ganha":
        tender.won_followup_status = request.form.get(
            "won_followup_status",
            tender.won_followup_status or "Em habilitação",
        )
    else:
        tender.won_followup_status = ""
    db.session.commit()
    flash("Licitação atualizada.", "success")
    return redirect(url_for("tender_detail", tender_id=tender.id))


@app.route("/tenders/<int:tender_id>/delete", methods=["POST"])
@login_required
def delete_tender(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    db.session.delete(tender)
    db.session.commit()
    flash("Licitação excluída com sucesso.", "success")
    return redirect(url_for("tenders"))


@app.route("/tenders/delete", methods=["POST"])
@login_required
def bulk_delete_tenders():
    ids = request.form.getlist("tender_ids")
    if not ids:
        flash("Selecione ao menos uma licitação para excluir.", "warning")
        return redirect(url_for("tenders"))
    tenders_to_delete = Tender.query.filter(Tender.id.in_(ids)).all()
    count = len(tenders_to_delete)
    for tender in tenders_to_delete:
        db.session.delete(tender)
    db.session.commit()
    flash(f"{count} licitação(ões) excluída(s).", "success")
    return redirect(url_for("tenders"))


@app.route("/ganhas")
@login_required
def won_tenders():
    followup = request.args.get("followup", "").strip()
    query = Tender.query.filter(Tender.status == "ganha")
    if followup:
        query = query.filter(Tender.won_followup_status == followup)
    tenders_list = query.order_by(Tender.dispute_date.desc()).all()
    return render_template("won_tenders.html", tenders=tenders_list, followup=followup)


@app.route("/ganhas/<int:tender_id>/update", methods=["POST"])
@login_required
def update_won_tender(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    tender.status = "ganha"
    tender.won_followup_status = request.form.get(
        "won_followup_status",
        tender.won_followup_status or "Em habilitação",
    )
    tender.won_notes = request.form.get("won_notes", tender.won_notes)
    db.session.commit()
    flash("Acompanhamento da licitação ganha atualizado.", "success")
    return redirect(url_for("won_tenders"))


@app.route("/tenders/<int:tender_id>/upload", methods=["POST"])
@login_required
def upload_attachment(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    file = request.files.get("attachment")
    if not file or not file.filename:
        flash("Selecione um arquivo para anexar.", "warning")
        return redirect(url_for("tender_detail", tender_id=tender.id))

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe = secure_filename(file.filename)
    saved_name = f"{timestamp}_{safe}"
    file.save(UPLOAD_DIR / saved_name)
    att = Attachment(tender_id=tender.id, original_name=file.filename, file_name=saved_name)
    db.session.add(att)
    db.session.commit()
    flash("Arquivo anexado com sucesso.", "success")
    return redirect(url_for("tender_detail", tender_id=tender.id))


@app.route("/items/<int:item_id>/update", methods=["POST"])
@login_required
def update_item(item_id: int):
    item = Item.query.get_or_404(item_id)
    item.supplier_id = request.form.get("supplier_id") or None
    item.item_number = request.form.get("item_number", item.item_number).strip()
    item.description = request.form.get("description", item.description).strip()
    item.quantity = float(request.form.get("quantity") or item.quantity)
    item.reference_value = float(request.form.get("reference_value") or item.reference_value)
    item.price_found = float(request.form.get("price_found") or 0)
    item.result_status = request.form.get("result_status", item.result_status)
    item.product_link = request.form.get("product_link", item.product_link).strip()
    item.quick_notes = request.form.get("quick_notes", item.quick_notes).strip()
    db.session.commit()
    flash("Item atualizado.", "success")
    return redirect(url_for("tender_detail", tender_id=item.tender_id, quick_item_id=item.id))


@app.route("/items/<int:item_id>/duplicate", methods=["POST"])
@login_required
def duplicate_item(item_id: int):
    item = Item.query.get_or_404(item_id)
    clone = Item(
        tender_id=item.tender_id,
        supplier_id=item.supplier_id,
        item_number=f"{item.item_number}-cópia",
        description=item.description,
        quantity=item.quantity,
        reference_value=item.reference_value,
        price_found=item.price_found,
        result_status="em disputa",
        product_link=item.product_link,
        quick_notes=item.quick_notes,
    )
    db.session.add(clone)
    db.session.commit()
    flash("Item duplicado.", "success")
    return redirect(url_for("tender_detail", tender_id=item.tender_id, quick_item_id=clone.id))


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_item(item_id: int):
    item = Item.query.get_or_404(item_id)
    tender_id = item.tender_id
    db.session.delete(item)
    db.session.commit()
    flash("Item excluído com sucesso.", "success")
    return redirect(url_for("tender_detail", tender_id=tender_id))


@app.route("/tenders/<int:tender_id>/items/delete", methods=["POST"])
@login_required
def bulk_delete_items(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    ids = request.form.getlist("item_ids")
    if not ids:
        flash("Selecione ao menos um item para excluir.", "warning")
        return redirect(url_for("tender_detail", tender_id=tender.id))
    items = Item.query.filter(Item.tender_id == tender.id, Item.id.in_(ids)).all()
    count = len(items)
    for item in items:
        db.session.delete(item)
    db.session.commit()
    flash(f"{count} item(ns) excluído(s).", "success")
    return redirect(url_for("tender_detail", tender_id=tender.id))


@app.route("/items/<int:item_id>/bid", methods=["POST"])
@login_required
def register_bid(item_id: int):
    item = Item.query.get_or_404(item_id)
    bid_value = float(request.form.get("bid_value") or 0)
    note = request.form.get("note", "").strip()
    item.last_bid = bid_value
    history = BidHistory(item_id=item.id, bid_value=bid_value, note=note)
    db.session.add(history)
    db.session.commit()
    flash(f"Lance {currency(bid_value)} registrado.", "success")
    origin = request.form.get("origin", "detail")
    if origin == "pregao":
        return redirect(url_for("pregao_mode", tender_id=item.tender_id, item_id=item.id))
    if origin == "quick":
        return redirect(url_for("quick_mode", tender_id=item.tender_id, item_id=item.id))
    return redirect(url_for("tender_detail", tender_id=item.tender_id, quick_item_id=item.id))


@app.route("/items/<int:item_id>/result", methods=["POST"])
@login_required
def set_item_result(item_id: int):
    item = Item.query.get_or_404(item_id)
    item.result_status = request.form.get("result_status", item.result_status)
    item.final_value = float(request.form.get("final_value")) if request.form.get("final_value") else item.last_bid
    if item.result_status == "ganho":
        item.tender.status = "ganha"
        normalize_won_status(item.tender)
    db.session.commit()
    flash("Resultado do item atualizado.", "success")
    return redirect(url_for("pregao_mode", tender_id=item.tender_id, item_id=item.id))


@app.route("/pregao/<int:tender_id>")
@login_required
def pregao_mode(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    item_id = request.args.get("item_id", type=int)
    selected_item = None
    items = Item.query.filter_by(tender_id=tender.id).order_by(Item.item_number.asc()).all()
    if item_id:
        selected_item = Item.query.filter_by(tender_id=tender.id, id=item_id).first()
    if not selected_item and items:
        selected_item = items[0]
    return render_template("pregao.html", tender=tender, items=items, selected_item=selected_item)


@app.route("/rapido/<int:tender_id>")
@login_required
def quick_mode(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    item_id = request.args.get("item_id", type=int)
    items = Item.query.filter_by(tender_id=tender.id).order_by(Item.item_number.asc()).all()
    selected_item = None
    if item_id:
        selected_item = Item.query.filter_by(tender_id=tender.id, id=item_id).first()
    if not selected_item and items:
        selected_item = items[0]
    return render_template("quick_mode.html", tender=tender, items=items, selected_item=selected_item)


@app.route("/suppliers", methods=["GET", "POST"])
@login_required
def suppliers():
    if request.method == "POST":
        supplier = Supplier(
            name=request.form.get("name", "").strip(),
            product_link=request.form.get("product_link", "").strip(),
            price_found=float(request.form.get("price_found") or 0),
            notes=request.form.get("notes", "").strip(),
        )
        db.session.add(supplier)
        db.session.commit()
        flash("Fornecedor cadastrado.", "success")
        return redirect(url_for("suppliers"))
    search = request.args.get("search", "").strip()
    query = Supplier.query
    if search:
        query = query.filter(Supplier.name.ilike(f"%{search}%"))
    suppliers_list = query.order_by(Supplier.name.asc()).all()
    return render_template("suppliers.html", suppliers=suppliers_list, search=search)


@app.route("/items/<int:item_id>/history")
@login_required
def item_history(item_id: int):
    item = Item.query.get_or_404(item_id)
    history = BidHistory.query.filter_by(item_id=item.id).order_by(BidHistory.created_at.desc()).all()
    return render_template("history.html", item=item, history=history)


@app.route("/export/tender/<int:tender_id>/csv")
@login_required
def export_tender_csv(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Item",
        "Descrição",
        "Quantidade",
        "Valor Referência",
        "Valor Encontrado",
        "Custo Máximo Permitido",
        "Diferença para o Limite",
        "Viabilidade",
        "Resultado",
        "Fornecedor",
        "Link",
        "Observações",
    ])
    for item in tender.items:
        writer.writerow([
            item.item_number,
            item.description,
            item.quantity,
            item.reference_value,
            item.price_found,
            item.max_cost_allowed,
            item.difference_to_limit,
            item.viability_label,
            item.result_status,
            item.supplier.name if item.supplier else "",
            item.product_link,
            item.quick_notes,
        ])
    mem = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    return send_file(
        mem,
        as_attachment=True,
        download_name=f"licitacao_{tender.edital_number}.csv",
        mimetype="text/csv",
    )


@app.route("/report/tender/<int:tender_id>")
@login_required
def report_tender(tender_id: int):
    tender = Tender.query.get_or_404(tender_id)
    return render_template("report.html", tender=tender)


@app.route("/backup")
@login_required
def backup_data():
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"):
        db_path = BASE_DIR / "licitacoes.db"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = BACKUP_DIR / f"licitacoes_backup_{timestamp}.db"
        if db_path.exists():
            shutil.copy2(db_path, backup_file)
            return send_file(backup_file, as_attachment=True, download_name=backup_file.name)
        flash("Banco de dados ainda não encontrado.", "warning")
        return redirect(url_for("dashboard"))

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Licitação",
        "Data da disputa",
        "Status",
        "Item",
        "Descrição",
        "Quantidade",
        "Valor Referência",
        "Valor Encontrado",
        "Custo Máximo Permitido",
        "Diferença",
        "Viabilidade",
        "Fornecedor",
        "Link",
        "Observações",
    ])

    for tender in Tender.query.order_by(Tender.dispute_date.asc()).all():
        for item in tender.items:
            writer.writerow([
                tender.edital_number,
                tender.dispute_date,
                tender.status,
                item.item_number,
                item.description,
                item.quantity,
                item.reference_value,
                item.price_found,
                item.max_cost_allowed,
                item.difference_to_limit,
                item.viability_label,
                item.supplier.name if item.supplier else "",
                item.product_link,
                item.quick_notes,
            ])

    mem = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    filename = f"licitacoes_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(mem, as_attachment=True, download_name=filename, mimetype="text/csv")


with app.app_context():
    ensure_schema()
    seed_admin()


if __name__ == "__main__":
    app.run(debug=True)