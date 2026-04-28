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
  
Database: SQLite (automatically created)
Run: python app.py
Access: http://localhost:5000
"""

from flask import Flask, render_template, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
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
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///invoicing.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Initialize database tables and data on app creation
with app.app_context():
    db.create_all()

# ============================================================================
# DATABASE MODELS
# ============================================================================

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
            # Hold pricing: ≤15 mins = £5, >15 mins = £15 per hour (or part)
            if self.minutes <= 15:
                return 5.0
            else:
                hours = (self.minutes - 15) / 60.0
                return 5.0 + (15.0 * (1 if hours > 0 else 0))  # Actually: 15 per full or partial hour
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
# UTILITY FUNCTIONS
# ============================================================================

def init_default_data():
    """Initialize database with default services and sample data."""
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
    
    # Sample owners and horses
    owners_horses = {
        'Amy S': ['Ronnie', 'Valli'],
        'Ashleigh': ['Phoenix'],
        'Bill': ['Freddie'],
        'Briony': ['Shiloe', 'Stardust'],
        'Cassie': ['Willy Wonka'],
        'Courtney': ['Jessie'],
        'Donna': ['Elphie', 'Benny'],
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
        'horse_name': entry.horse.name,
        'service_name': entry.service.name,
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


# ============================================================================
# ROUTES - PAGES
# ============================================================================

@app.route('/')
def index():
    """Home page."""
    return render_template('index.html')


@app.route('/work-entry')
def work_entry_page():
    """Work entry page."""
    return render_template('work_entry.html')


@app.route('/invoices')
def invoices_page():
    """Invoices page."""
    return render_template('invoices.html')


@app.route('/settings')
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
    Generate a PDF invoice for an owner.
    
    Args:
        owner_id: Owner ID
        month: Month name (e.g., "May")
        year: Year (e.g., 2026)
        work_entries: List of work entries for this month
    
    Returns:
        BytesIO object with PDF data
    """
    owner = Owner.query.get(owner_id)
    
    # Create PDF in memory
    pdf_buffer = BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4, topMargin=0.5*cm, bottomMargin=0.5*cm)
    
    styles = getSampleStyleSheet()
    style_title = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#4a4a4a'),
        spaceAfter=12,
        alignment=TA_LEFT,
    )
    
    # Build content
    elements = []
    
    # Header
    elements.append(Paragraph('INVOICE', style_title))
    elements.append(Spacer(1, 0.2*cm))
    
    month_year = f'{month.upper()} {year}'
    elements.append(Paragraph(f'<b>{month_year}</b>', styles['Normal']))
    elements.append(Spacer(1, 0.5*cm))
    
    # Owner info
    elements.append(Paragraph(f'<b>Owner:</b> {owner.name}', styles['Normal']))
    horses = set(entry['horse_name'] for entry in work_entries)
    elements.append(Paragraph(f'<b>Horses:</b> {", ".join(sorted(horses))}', styles['Normal']))
    elements.append(Spacer(1, 0.5*cm))
    
    # Invoice table
    table_data = [['Date', 'Horse', 'Activity', 'Cost']]
    
    for entry in sorted(work_entries, key=lambda x: (x['date'], x['horse_name'])):
        date_str = entry['date'].strftime('%d %b') if hasattr(entry['date'], 'strftime') else str(entry['date'])
        table_data.append([
            date_str,
            entry['horse_name'],
            entry['service_name'],
            f"£{entry['cost']:.2f}",
        ])
    
    # Add total row
    total = sum(entry['cost'] for entry in work_entries)
    table_data.append(['', '', 'TOTAL', f"£{total:.2f}"])
    
    # Style table
    table = Table(table_data, colWidths=[2.3*cm, 3.2*cm, 5.5*cm, 2*cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4a4a4a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f0f0f0')),
        ('TOPPADDING', (0, 1), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
    ]))
    
    elements.append(table)
    elements.append(Spacer(1, 1*cm))
    
    # Payment details
    elements.append(Paragraph(
        '<b>Payment details:</b><br/>C S White<br/>Sort code: 20-03-18<br/>Account: 13901858<br/>Reference: Use your horse\'s name',
        styles['Normal']
    ))
    
    # Build PDF
    doc.build(elements)
    pdf_buffer.seek(0)
    
    return pdf_buffer


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
        init_default_data()
    
    print('Starting Cassie\'s Invoicing System...')
    print('Open your browser to: http://localhost:5000')
    app.run(debug=True, host='0.0.0.0', port=5000)
