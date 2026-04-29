"""
Cassie's Invoicing System
=========================
A Flask web application for managing horse grooming services and generating invoices.

Features:
  • Work entry (date, horse, services)
  • Real-time invoice preview
  • PDF export
  • Invoice history
  • Owner/horse management
  • Pricing management
  
Database: PostgreSQL (Railway.app) or SQLite (local development)
Run: python app.py
Access: http://localhost:5000
"""

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, date
from io import BytesIO
import json
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

# ============================================================================
# CONFIGURATION
# ============================================================================

app = Flask(__name__)

# Database configuration: PostgreSQL (Railway) or SQLite (local)
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Fix for SQLAlchemy 1.4+ compatibility (Railway uses postgres://)
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
    }
    print(f'✅ Using PostgreSQL: {database_url[:30]}...')
else:
    # Local development: SQLite
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///invoicing.db'
    print('✅ Using SQLite (local development)')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

db = SQLAlchemy(app)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Create tables on app startup (runs with Gunicorn too!)
with app.app_context():
    db.create_all()

# ============================================================================
# DATABASE MODELS
# ============================================================================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def __repr__(self):
        return f'<User {self.username}>'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.before_request
def check_login():
    """Require login for all routes except /login"""
    if request.path.startswith('/api/') and not current_user.is_authenticated:
        return jsonify({'error': 'Unauthorized'}), 401


class Owner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    horses = db.relationship('Horse', backref='owner', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Owner {self.name}>'


class Horse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('owner.id'), nullable=False)
    active = db.Column(db.Boolean, default=True)
    work_entries = db.relationship('WorkEntry', backref='horse', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Horse {self.name} ({self.owner.name})>'


class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    base_price = db.Column(db.Float, nullable=False)
    requires_time = db.Column(db.Boolean, default=False)  # True for "Hold", False for fixed services
    
    def __repr__(self):
        return f'<Service {self.code}>'


class WorkEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    horse_id = db.Column(db.Integer, db.ForeignKey('horse.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=False)
    minutes = db.Column(db.Integer, default=0)  # Only used for time-based services (Hold)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    service = db.relationship('Service', backref='work_entries')
    
    def __repr__(self):
        return f'<WorkEntry {self.horse.name} - {self.service.code} on {self.date}>'
    
    def calculate_cost(self):
        """Calculate cost based on service type and duration."""
        if self.service.requires_time:
            # Hold pricing - use the global calculate_hold_price function
            return calculate_hold_price(self.minutes)
        else:
            # Fixed price
            return self.service.base_price


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('owner.id'), nullable=False)
    month = db.Column(db.String(20), nullable=False)  # "May 2026"
    year = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    pdf_data = db.Column(db.LargeBinary)  # Store PDF as binary
    
    owner = db.relationship('Owner', backref='invoices')
    
    def __repr__(self):
        return f'<Invoice {self.owner.name} - {self.month} {self.year}>'


# ============================================================================
# INITIALIZE DATABASE TABLES (after models are defined)
# ============================================================================

with app.app_context():
    db.create_all()

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def init_default_data():
    """Initialize database with services and Cassie's real owner/horse data."""
    if db.session.query(Service).first() is not None:
        return  # Already initialized
    
    # Services from Cassie's actual price list
    services = [
        Service(code='FL', name='Full Livery', base_price=15.0, requires_time=False),
        Service(code='PL', name='Part Livery', base_price=12.0, requires_time=False),
        Service(code='BI', name='Bring In', base_price=3.0, requires_time=False),
        Service(code='TO', name='Turn Out', base_price=3.0, requires_time=False),
        Service(code='RUG', name='Rug', base_price=1.0, requires_time=False),
        Service(code='FP', name='Feet pick', base_price=1.0, requires_time=False),
        Service(code='SO', name='Skip out only', base_price=5.0, requires_time=False),
        Service(code='SHAY', name='Skip out, Hay, Water', base_price=8.0, requires_time=False),
        Service(code='FMO', name='Full Muck Out', base_price=12.0, requires_time=False),
        Service(code='FS', name='Field Service (Feed, Rug, Muzzle, Fly spray)', base_price=3.0, requires_time=False),
        Service(code='H', name='Holding (farrier/vet/other)', base_price=5.0, requires_time=True),  # Time-based
    ]
    
    for service in services:
        db.session.add(service)
    
    # Cassie's real owners and horses (from CSV)
    owners_horses = {
        'Amy S': ['Ronnie'],
        'Ashliegh': ['Valli'],
        'Bethany': ['Phoenix'],
        'Bill': ['Freddie'],
        'Briony': ['Shiloe', 'Stardust'],
        'Cassie': ['Willy Wonka'],
        'Courtney': ['Jessie'],
        'Donna': ['Elphie', 'Benny', 'Maisie'],
        'Emma': ['Lexi'],
        'Harleigh': ['Jack', 'Mac', 'Louis'],
        'Heidi': ['Freddie1', 'Waffle'],
        'Jacquie': ['Sully', 'B'],
        'Jade': ['George'],
        'Jess': ['Tilly'],
        'Jess S': ['Jude'],
        'Joanne': ['Maverick'],
        'Julie': ['Amy', 'Tom'],
        'Kelly': ['Emerald', 'Mike'],
        'Lauren': ['Nola'],
        'Lindsay': ['Sonic', 'Didi'],
        'Lyn': ['Mystique'],
        'Mark': ['Lenny'],
        'Michelle': ['Misty'],
        'Natalie & Jason': ['Hodor', 'Rupert', 'William', 'Shaun', 'Jacko', 'Cassius', 'Dan'],
        'Natalie M': ['Jasper', 'Aero', 'Rio', 'Fred'],
        'Nikki': ['Blossom'],
        'Nikki B': ['Charlie'],
        'Olivia': ['Sid', 'Dottie'],
        'Pauline': ['Stan'],
        'Purdy': ['Belle'],
        'Richard': ['Echo'],
        'Ruth': ['Oreo', 'Horis', 'Porter'],
        'Sam': ['Gem'],
        'Samantha Jackson': ['Jakus'],
        'Sandra': ['Bear'],
        'Sarah': ['Abe'],
        'Serena': ['Busker'],
        'Shannon': ['Sadie', 'Billy'],
        'Sharron': ['Flo'],
        'Sophie': ['Tammy', 'Billy2'],
        'Steph': ['Abs', 'Crumble'],
        'Sue': ['Feargal', 'Dulcie'],
        'Tracy': ['Saffy', 'Fancy'],
    }
    
    for owner_name, horse_names in owners_horses.items():
        owner = Owner(name=owner_name)
        db.session.add(owner)
        db.session.flush()
        
        for horse_name in horse_names:
            horse = Horse(name=horse_name, owner_id=owner.id)
            db.session.add(horse)
    
    db.session.commit()


def calculate_hold_price(minutes):
    """
    Calculate Hold service price based on duration (from Cassie's price list).
    - Up to 15 mins: £5
    - Up to 30 mins: £10
    - Up to 1 hour (60 mins): £15
    - Each additional hour or part thereafter: £15
    """
    if minutes <= 15:
        return 5.0
    elif minutes <= 30:
        return 10.0
    elif minutes <= 60:
        return 15.0
    else:
        # Over 60 minutes: £15 for first hour + £15 per additional hour or part
        remaining_minutes = minutes - 60
        additional_hours = (remaining_minutes + 59) // 60  # Round up to nearest hour
        return 15.0 + (15.0 * additional_hours)


# ============================================================================
# ROUTES - API
# ============================================================================

@app.route('/api/owners', methods=['GET'])
def get_owners():
    """Get all owners with their horses."""
    owners = Owner.query.all()
    return jsonify([{
        'id': owner.id,
        'name': owner.name,
        'horses': [{
            'id': horse.id,
            'name': horse.name,
        } for horse in owner.horses if horse.active]
    } for owner in owners])


@app.route('/api/services', methods=['GET'])
def get_services():
    """Get all services."""
    services = Service.query.all()
    return jsonify([{
        'id': service.id,
        'code': service.code,
        'name': service.name,
        'base_price': service.base_price,
        'requires_time': service.requires_time,
    } for service in services])


@app.route('/api/work-entries', methods=['GET'])
def get_work_entries():
    """Get work entries for a given date range."""
    start_date_str = request.args.get('start')
    end_date_str = request.args.get('end')
    
    if not start_date_str or not end_date_str:
        return jsonify([])
    
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    entries = WorkEntry.query.filter(
        WorkEntry.date >= start_date,
        WorkEntry.date <= end_date
    ).all()
    
    return jsonify([{
        'id': entry.id,
        'date': entry.date.isoformat(),
        'horse_id': entry.horse_id,
        'horse_name': entry.horse.name,
        'owner_id': entry.horse.owner_id,
        'owner_name': entry.horse.owner.name,
        'service_code': entry.service.code,
        'service_name': entry.service.name,
        'minutes': entry.minutes,
        'cost': entry.calculate_cost(),
    } for entry in entries])


@app.route('/api/work-entries', methods=['POST'])
def add_work_entry():
    """Add a new work entry."""
    data = request.json
    
    entry = WorkEntry(
        date=datetime.strptime(data['date'], '%Y-%m-%d').date(),
        horse_id=data['horse_id'],
        service_id=data['service_id'],
        minutes=data.get('minutes', 0),
    )
    
    db.session.add(entry)
    db.session.commit()
    
    return jsonify({
        'id': entry.id,
        'date': entry.date.isoformat(),
        'horse_id': entry.horse_id,
        'horse_name': entry.horse.name,
        'owner_id': entry.horse.owner_id,
        'owner_name': entry.horse.owner.name,
        'service_code': entry.service.code,
        'service_name': entry.service.name,
        'minutes': entry.minutes,
        'cost': entry.calculate_cost(),
    }), 201


@app.route('/api/work-entries/<int:entry_id>', methods=['DELETE'])
def delete_work_entry(entry_id):
    """Delete a work entry."""
    entry = WorkEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    return '', 204


@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    """Get invoice history."""
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    return jsonify([{
        'id': invoice.id,
        'owner_name': invoice.owner.name,
        'month': invoice.month,
        'year': invoice.year,
        'created_at': invoice.created_at.isoformat(),
    } for invoice in invoices])


@app.route('/api/invoices/generate-owner/<int:owner_id>', methods=['POST'])
def generate_invoice_for_owner(owner_id):
    """Generate invoice for a specific owner for a given month."""
    data = request.json
    month = int(data.get('month', 0))
    year = int(data.get('year', 0))
    
    if not month or not year:
        return jsonify({'error': 'Month and year required'}), 400
    
    owner = Owner.query.get_or_404(owner_id)
    
    try:
        # Get work entries for this owner in this month
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)
        
        work_entries = WorkEntry.query.filter(
            WorkEntry.date >= start_date,
            WorkEntry.date <= end_date,
            WorkEntry.horse.has(Horse.owner_id == owner_id)
        ).all()
        
        if not work_entries:
            return jsonify({
                'success': False,
                'error': f'{owner.name} has no work entries for this month'
            }), 400
        
        # Generate PDF
        pdf_buffer = generate_invoice_pdf(owner_id, month, year, work_entries)
        
        # Save invoice record
        invoice = Invoice(
            owner_id=owner_id,
            month=month,
            year=year,
            pdf_data=pdf_buffer.getvalue() if pdf_buffer else None
        )
        db.session.add(invoice)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'generated': 1,
            'errors': []
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices/generate', methods=['POST'])
def generate_invoices():
    """Generate invoices for all owners for a given month."""
    data = request.json
    month = int(data.get('month')) + 1  # Convert 0-indexed to 1-indexed (0=Jan, 11=Dec)
    year = int(data.get('year'))
    
    if not month or not year:
        return jsonify({'error': 'Month and year required'}), 400
    
    # Get all owners
    owners = Owner.query.all()
    generated_count = 0
    errors = []
    
    for owner in owners:
        try:
            # Get work entries for this owner in this month
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = date(year, month + 1, 1) - timedelta(days=1)
            
            work_entries = WorkEntry.query.filter(
                WorkEntry.date >= start_date,
                WorkEntry.date <= end_date,
                WorkEntry.horse.has(Horse.owner_id == owner.id)
            ).all()
            
            # Only generate if there are work entries
            if work_entries:
                # Generate PDF
                pdf_data = generate_invoice_pdf(owner.id, month, year, work_entries)
                
                # Save invoice record
                invoice = Invoice(
                    owner_id=owner.id,
                    month=month,
                    year=year,
                    pdf_data=pdf_data
                )
                db.session.add(invoice)
                generated_count += 1
        except Exception as e:
            errors.append(f"{owner.name}: {str(e)}")
    
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    
    return jsonify({
        'success': True,
        'generated': generated_count,
        'errors': errors
    })


@app.route('/api/invoices/generate-owner/<int:owner_id>', methods=['POST'])
def generate_owner_invoice(owner_id):
    """Generate invoice for a specific owner for a given month."""
    data = request.json
    month = int(data.get('month')) + 1  # Convert 0-indexed to 1-indexed
    year = int(data.get('year'))
    
    if not month or not year:
        return jsonify({'error': 'Month and year required'}), 400
    
    owner = Owner.query.get_or_404(owner_id)
    generated_count = 0
    errors = []
    
    try:
        # Get work entries for this owner in this month
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)
        
        work_entries = WorkEntry.query.filter(
            WorkEntry.date >= start_date,
            WorkEntry.date <= end_date,
            WorkEntry.horse.has(Horse.owner_id == owner.id)
        ).all()
        
        # Only generate if there are work entries
        if work_entries:
            # Generate PDF
            pdf_buffer = generate_invoice_pdf(owner.id, month, year, work_entries)
            
            # Save invoice record
            invoice = Invoice(
                owner_id=owner.id,
                month=month,
                year=year,
                pdf_data=pdf_buffer.getvalue() if pdf_buffer else None
            )
            db.session.add(invoice)
            db.session.commit()
            generated_count = 1
        else:
            return jsonify({
                'success': False,
                'error': f'No work entries found for {owner.name} in {month}/{year}'
            }), 400
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    
    return jsonify({
        'success': True,
        'generated': generated_count,
        'errors': errors
    })


@app.route('/api/invoices/<int:invoice_id>/pdf', methods=['GET'])
def get_invoice_pdf(invoice_id):
    """Download a saved invoice PDF."""
    invoice = Invoice.query.get_or_404(invoice_id)
    
    if not invoice.pdf_data:
        return 'PDF not found', 404
    
    return send_file(
        BytesIO(invoice.pdf_data),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'{invoice.owner.name}_{invoice.month}_{invoice.year}.pdf'
    )


@app.route('/api/invoices/download-all', methods=['GET'])
def download_all_invoices():
    """Download all invoices for a month as a ZIP file."""
    import zipfile
    
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)
    
    if not month or not year:
        return {'error': 'Month and year required'}, 400
    
    # Get all invoices for this month/year
    invoices = Invoice.query.filter(
        Invoice.month == month,
        Invoice.year == year
    ).all()
    
    if not invoices:
        return {'error': 'No invoices found for this month'}, 404
    
    # Create ZIP file in memory
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for invoice in invoices:
            if invoice.pdf_data:
                filename = f'{invoice.owner.name}_{month:02d}_{year}.pdf'
                zip_file.writestr(filename, invoice.pdf_data)
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'invoices_{month:02d}_{year}.zip'
    )


# ============================================================================
# ROUTES - AUTHENTICATION
# ============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Invalid username or password')
    
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    """Logout and redirect to login."""
    logout_user()
    return redirect(url_for('login'))


@app.route('/setup-default-user')
def setup_default_user():
    """Create default user if it doesn't exist. Remove this after setup!"""
    with app.app_context():
        if User.query.filter_by(username='cassie').first():
            return jsonify({'message': 'Default user already exists'}), 200
        
        default_user = User(username='cassie')
        default_user.set_password('cassie123')
        db.session.add(default_user)
        db.session.commit()
        
        return jsonify({
            'message': 'Default user created successfully',
            'username': 'cassie',
            'password': 'cassie123'
        }), 201


# ============================================================================
# ROUTES - PAGES
# ============================================================================

@app.route('/')
@login_required
def index():
    """Home page."""
    return render_template('index.html')


@app.route('/work-entry')
@login_required
def work_entry_page():
    """Work entry page."""
    return render_template('work_entry.html')


@app.route('/invoices')
@login_required
def invoices_page():
    """Invoices page."""
    return render_template('invoices.html')


@app.route('/settings')
@login_required
def settings_page():
    """Settings page (owners, horses, services, pricing)."""
    return render_template('settings.html')



# ============================================================================
# API ROUTES - SETTINGS / MANAGEMENT
# ============================================================================

@app.route('/api/owners', methods=['POST'])
def create_owner():
    """Create a new owner."""
    data = request.json
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Owner name required'}), 400
    
    if Owner.query.filter_by(name=name).first():
        return jsonify({'error': 'Owner already exists'}), 400
    
    owner = Owner(name=name)
    db.session.add(owner)
    db.session.commit()
    
    return jsonify({
        'id': owner.id,
        'name': owner.name,
    }), 201


@app.route('/api/owners/<int:owner_id>', methods=['PUT'])
def update_owner(owner_id):
    """Update owner name."""
    owner = Owner.query.get_or_404(owner_id)
    data = request.json
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Owner name required'}), 400
    
    existing = Owner.query.filter_by(name=name).first()
    if existing and existing.id != owner_id:
        return jsonify({'error': 'Owner name already exists'}), 400
    
    owner.name = name
    db.session.commit()
    
    return jsonify({
        'id': owner.id,
        'name': owner.name,
    })


@app.route('/api/owners/<int:owner_id>', methods=['DELETE'])
def delete_owner(owner_id):
    """Delete an owner (and cascade to horses)."""
    owner = Owner.query.get_or_404(owner_id)
    
    has_work = WorkEntry.query.join(Horse).filter(Horse.owner_id == owner_id).first()
    if has_work:
        return jsonify({'error': 'Cannot delete owner with work entries. Delete entries first.'}), 400
    
    db.session.delete(owner)
    db.session.commit()
    
    return '', 204


@app.route('/api/horses', methods=['GET'])
def get_horses():
    """Get all horses."""
    horses = Horse.query.all()
    return jsonify([{
        'id': horse.id,
        'name': horse.name,
        'owner_id': horse.owner_id,
        'owner_name': horse.owner.name if horse.owner else None,
    } for horse in horses])


@app.route('/api/horses', methods=['POST'])
def create_horse():
    """Create a new horse."""
    data = request.json
    name = data.get('name', '').strip()
    owner_id = data.get('owner_id')
    
    if not name:
        return jsonify({'error': 'Horse name required'}), 400
    
    if not owner_id:
        return jsonify({'error': 'Owner required'}), 400
    
    owner = Owner.query.get_or_404(owner_id)
    
    horse = Horse(name=name, owner_id=owner_id)
    db.session.add(horse)
    db.session.commit()
    
    return jsonify({
        'id': horse.id,
        'name': horse.name,
        'owner_id': horse.owner_id,
        'owner_name': horse.owner.name,
    }), 201


@app.route('/api/horses/<int:horse_id>', methods=['PUT'])
def update_horse(horse_id):
    """Update horse name or owner."""
    horse = Horse.query.get_or_404(horse_id)
    data = request.json
    
    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'Horse name required'}), 400
        horse.name = name
    
    if 'owner_id' in data:
        owner_id = data['owner_id']
        owner = Owner.query.get_or_404(owner_id)
        horse.owner_id = owner_id
    
    if 'active' in data:
        horse.active = data['active']
    
    db.session.commit()
    
    return jsonify({
        'id': horse.id,
        'name': horse.name,
        'owner_id': horse.owner_id,
        'owner_name': horse.owner.name,
        'active': horse.active,
    })


@app.route('/api/horses/<int:horse_id>', methods=['DELETE'])
def delete_horse(horse_id):
    """Delete a horse (soft delete - mark as inactive, or hard delete if no work)."""
    horse = Horse.query.get_or_404(horse_id)
    
    has_work = WorkEntry.query.filter_by(horse_id=horse_id).first()
    if has_work:
        horse.active = False
        db.session.commit()
        return jsonify({'message': 'Horse marked as inactive (still appears in history)'}), 200
    else:
        db.session.delete(horse)
        db.session.commit()
        return '', 204


@app.route('/api/services', methods=['POST'])
def create_service():
    """Create a new service."""
    data = request.json
    code = data.get('code', '').strip().upper()
    name = data.get('name', '').strip()
    base_price = data.get('base_price')
    requires_time = data.get('requires_time', False)
    
    if not code or not name or base_price is None:
        return jsonify({'error': 'Code, name, and price required'}), 400
    
    if Service.query.filter_by(code=code).first():
        return jsonify({'error': 'Service code already exists'}), 400
    
    service = Service(
        code=code,
        name=name,
        base_price=base_price,
        requires_time=requires_time,
    )
    db.session.add(service)
    db.session.commit()
    
    return jsonify({
        'id': service.id,
        'code': service.code,
        'name': service.name,
        'base_price': service.base_price,
        'requires_time': service.requires_time,
    }), 201


@app.route('/api/services/<int:service_id>', methods=['PUT'])
def update_service(service_id):
    """Update service details."""
    service = Service.query.get_or_404(service_id)
    data = request.json
    
    if 'name' in data:
        service.name = data['name'].strip()
    
    if 'base_price' in data:
        service.base_price = float(data['base_price'])
    
    if 'requires_time' in data:
        service.requires_time = bool(data['requires_time'])
    
    db.session.commit()
    
    return jsonify({
        'id': service.id,
        'code': service.code,
        'name': service.name,
        'base_price': service.base_price,
        'requires_time': service.requires_time,
    })


@app.route('/api/services/<int:service_id>', methods=['DELETE'])
def delete_service(service_id):
    """Delete a service (only if not used)."""
    service = Service.query.get_or_404(service_id)
    
    has_work = WorkEntry.query.filter_by(service_id=service_id).first()
    if has_work:
        return jsonify({'error': 'Cannot delete service with work entries'}), 400
    
    db.session.delete(service)
    db.session.commit()
    
    return '', 204


# ============================================================================
# PDF GENERATION
# ============================================================================

def generate_invoice_pdf(owner_id, month, year, work_entries):
    """
    Generate a PDF invoice for an owner with multi-horse layout.
    """
    from reportlab.lib.enums import TA_LEFT
    owner = Owner.query.get(owner_id)
    
    # Convert month number to month name
    month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    month_name = month_names[int(month)] if isinstance(month, (int, str)) else str(month)
    
    # Create PDF in memory
    pdf_buffer = BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4, topMargin=0.5*cm, bottomMargin=0.5*cm,
                           leftMargin=0.5*cm, rightMargin=0.5*cm)
    
    styles = getSampleStyleSheet()
    style_title = ParagraphStyle(
        'CustomTitle',
        parent=styles['Normal'],
        fontSize=14,
        textColor=colors.HexColor('#2a2a2a'),
        spaceAfter=0,
        alignment=TA_LEFT,
    )
    style_company = ParagraphStyle(
        'Company',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#2a2a2a'),
        spaceAfter=8,
    )
    
    # Build content
    elements = []
    
    # Header: INVOICE (left) and Company (right) on same line - use Paragraph with tabs/spacing
    # Create a simple two-column effect without table borders
    from reportlab.lib.enums import TA_RIGHT
    
    header_style_left = ParagraphStyle(
        'HeaderLeft',
        parent=styles['Normal'],
        fontSize=14,
        fontName='Helvetica-Bold',
        alignment=TA_LEFT,
    )
    
    header_style_right = ParagraphStyle(
        'HeaderRight',
        parent=styles['Normal'],
        fontSize=14,
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT,
    )
    
    # Header: INVOICE (left) and Company (right) - use Paragraphs without indent
    invoice_style = ParagraphStyle(
        'InvoiceHeader',
        parent=styles['Normal'],
        fontSize=14,
        fontName='Helvetica-Bold',
        leftIndent=0,  # No indent - INVOICE at left
    )
    
    company_style = ParagraphStyle(
        'CompanyHeader',
        parent=styles['Normal'],
        fontSize=14,
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT,
    )
    
    # INVOICE on left and Company on right (separate paragraph)
    invoice_para = Paragraph('<b>INVOICE</b>', invoice_style)
    company_para = Paragraph('<b>Cassie White Equestrian Services</b>', company_style)
    
    # Create a simple table to position them side by side
    header_layout = Table([
        [invoice_para, company_para]
    ], colWidths=[10*cm, 10*cm])
    header_layout.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, 0), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, 0), 0),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 0),
        ('LEFTPADDING', (0, 0), (-1, 0), 0),
        ('RIGHTPADDING', (0, 0), (-1, 0), 0),
    ]))
    elements.append(header_layout)
    elements.append(Spacer(1, 0.15*cm))
    
    month_year = f'{month_name.upper()} {year}'
    elements.append(Paragraph(f'<b>{month_year}</b>', styles['Normal']))
    elements.append(Spacer(1, 0.2*cm))
    
    # Owner info - no indent, at left margin
    elements.append(Paragraph(f'<b>Owner:</b> {owner.name}', styles['Normal']))
    horses = sorted(set(entry.horse.name for entry in work_entries))
    elements.append(Paragraph(f'<b>Horses:</b> {", ".join(horses)}', styles['Normal']))
    elements.append(Spacer(1, 0.3*cm))
    
    # Multi-horse layout: group by date and horse
    by_date = {}
    for entry in work_entries:
        date_key = entry.date.isoformat()
        if date_key not in by_date:
            by_date[date_key] = {}
        if entry.horse.name not in by_date[date_key]:
            by_date[date_key][entry.horse.name] = []
        by_date[date_key][entry.horse.name].append(entry)
    
    # Build table: Date | Horse1(Service|Price) | Horse2(Service|Price) | etc.
    # Each horse gets 2 columns: Service name | Price
    table_data = []
    
    # Header row 1: Horse names (will be merged across 2 columns each)
    # Need to interleave horse names with empty strings for the Price columns they'll span
    header_row_1 = ['Date']
    for horse in horses:
        header_row_1.append(horse)
        header_row_1.append('')  # Empty cell for Price column (will be spanned)
    table_data.append(header_row_1)
    
    # Header row 2: Service | Price labels for each horse
    header_row_2 = [''] + ['Service', 'Price'] * len(horses)
    table_data.append(header_row_2)
    
    # Data rows
    for date_key in sorted(by_date.keys()):
        # Format date as DD-MM-YY
        from datetime import datetime
        date_obj = datetime.fromisoformat(date_key).date()
        date_str = date_obj.strftime('%d-%m-%y')
        
        # Get all entries for this date, grouped by horse
        entries_by_horse = {h: [] for h in horses}
        if date_key in by_date:
            for horse in horses:
                if horse in by_date[date_key]:
                    entries_by_horse[horse] = sorted(by_date[date_key][horse], key=lambda x: x.id)
        
        # Find max services on this date across all horses
        max_services = max([len(entries_by_horse[h]) for h in horses]) if horses else 1
        
        # Add a row for each service
        for service_idx in range(max_services):
            row = [date_str if service_idx == 0 else '']  # Date only in first row
            
            for horse in horses:
                if service_idx < len(entries_by_horse[horse]):
                    entry = entries_by_horse[horse][service_idx]
                    
                    # Shorten service names
                    service_name = entry.service.name
                    if entry.service.code == "H":
                        service_name = f'Hold ({entry.minutes} min)'
                    elif service_name == "Holding (farrier/vet/other)":
                        service_name = f'Hold ({entry.minutes} min)'
                    elif service_name == "Field Service (Feed, Rug, Muzzle, Fly spray)":
                        service_name = "Field Service"
                    elif service_name == "Skip out only":
                        service_name = "Skip Out"
                    
                    price = f'£{entry.calculate_cost():.2f}'
                    row.append(service_name)
                    row.append(price)
                else:
                    row.append('')
                    row.append('')
            table_data.append(row)
    
    # Add subtotal row per horse
    subtotal_row = ['Subtotal']
    for horse in horses:
        horse_entries = [e for e in work_entries if e.horse.name == horse]
        horse_total = sum(e.calculate_cost() for e in horse_entries)
        subtotal_row.append('')  # Empty service cell
        subtotal_row.append(f'£{horse_total:.2f}')
    table_data.append(subtotal_row)
    
    # Table column widths: Date | (Service, Price) pairs for each horse
    # A4 width = 21cm, with 0.5cm margins = 20cm available
    available_width = A4[0] - 1*cm  # 20cm
    date_col_width = 1.8*cm
    pair_width = (available_width - date_col_width) / len(horses)
    service_col_width = pair_width * 0.65  # 65% for service name
    price_col_width = pair_width * 0.35    # 35% for price
    col_widths = [date_col_width] + [service_col_width, price_col_width] * len(horses)
    table = Table(table_data, colWidths=col_widths)
    
    # Build style list with dynamic horse header merging
    style_list = [
        # Header row 1 (Horse names) - dark background
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4a4a4a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),  # Date header
        # Merge horse name headers across Service+Price columns
    ]
    
    # Add SPAN for each horse (each spans 2 columns: Service | Price)
    for i in range(len(horses)):
        col_start = 1 + (i * 2)
        col_end = col_start + 1
        style_list.append(('SPAN', (col_start, 0), (col_end, 0)))
        style_list.append(('ALIGN', (col_start, 0), (col_end, 0), 'CENTER'))
        # Align service columns (odd) LEFT and price columns (even) RIGHT
        style_list.append(('ALIGN', (col_start, 2), (col_start, -2), 'LEFT'))   # Service: LEFT
        style_list.append(('ALIGN', (col_end, 2), (col_end, -2), 'RIGHT'))      # Price: RIGHT
        # Also align subtotal row properly
        style_list.append(('ALIGN', (col_start, -1), (col_start, -1), 'LEFT'))   # Service blank
        style_list.append(('ALIGN', (col_end, -1), (col_end, -1), 'RIGHT'))      # Price
    
    # Continue with rest of styles
    style_list.extend([
        # Header row 2 (Service | Price) - lighter background
        ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#f0f0f0')),
        ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 1), (-1, 1), 7),
        ('ALIGN', (0, 1), (-1, 1), 'CENTER'),
        # Data rows - need to alternate LEFT for services, RIGHT for prices
        ('ALIGN', (0, 2), (0, -2), 'LEFT'),   # Date column: left
        # Subtotal row
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f0f0f0')),
        ('ALIGN', (0, -1), (0, -1), 'LEFT'),
        ('ALIGN', (1, -1), (-1, -1), 'RIGHT'),
        # Grid
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        # Padding - reduced for tighter fit
        ('TOPPADDING', (0, 2), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 2), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, 1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 1), 5),
        # Font sizes - reduced
        ('FONTSIZE', (0, 0), (-1, 0), 8),  # Horse name headers
        ('FONTSIZE', (0, 2), (-1, -2), 7),  # Data rows
        ('FONTSIZE', (0, -1), (-1, -1), 7),  # Subtotal row
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ])
    
    table.setStyle(TableStyle(style_list))
    
    elements.append(table)
    elements.append(Spacer(1, 0.4*cm))
    
    # Grand total
    total = sum(entry.calculate_cost() for entry in work_entries)
    elements.append(Paragraph(f'<b>TOTAL: £{total:.2f}</b>', styles['Normal']))
    elements.append(Spacer(1, 0.4*cm))
    
    # Payment details
    elements.append(Paragraph('<b>Invoice Due For Immediate Payment Please:</b>', styles['Normal']))
    elements.append(Paragraph('Cassie White Equestrian Services Ltd<br/>Sort code: 60-83-71<br/>Account number: 62430438', styles['Normal']))
    elements.append(Paragraph('<i>Use your horse\'s name as a reference</i>', styles['Normal']))
    elements.append(Spacer(1, 0.3*cm))
    
    # Terms - bold
    terms_style = ParagraphStyle(
        'Terms',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#2a2a2a'),
    )
    elements.append(Paragraph(
        '<b>Invoices to be paid upon receipt. Late or non-payment may result in services being refused.</b>',
        terms_style
    ))
    
    # Build PDF
    doc.build(elements)
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()


# ============================================================================
# CLI / MANAGEMENT
# ============================================================================

@app.cli.command()
def init_db():
    """Initialize the database."""
    db.create_all()
    init_default_data()
    print('Database initialized!')


# Auto-initialize database on first run
@app.before_request
def init_on_first_run():
    """Initialize default data if not already done."""
    try:
        # Always try to initialize - the function checks if already done
        init_default_data()
    except Exception as e:
        # Log but don't crash
        print(f"Warning: Could not initialize data: {e}")


@app.cli.command()
def load_excel():
    """Load data from Excel workbook (optional)."""
    print('Excel import not yet implemented.')


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def server_error(error):
    return jsonify({'error': 'Server error'}), 500


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        
        # Create default user if it doesn't exist
        if not User.query.filter_by(username='cassie').first():
            default_user = User(username='cassie')
            default_user.set_password('cassie123')
            db.session.add(default_user)
            db.session.commit()
            print('✅ Created default user: cassie / cassie123')
        
        init_default_data()
    
    print('Starting Cassie\'s Invoicing System...')
    print('Open your browser to: http://localhost:5000')
    app.run(debug=True, host='0.0.0.0', port=5000)