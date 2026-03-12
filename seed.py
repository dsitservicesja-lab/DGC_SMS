"""Seed the database with initial admin user and demo data."""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app, db
from app.models import User, Role, Branch


def seed():
    app = create_app('development')
    with app.app_context():
        db.create_all()

        # Check if admin already exists
        if User.query.filter_by(username='admin').first():
            print('Database already seeded (admin user exists).')
            return

        # Admin user
        admin = User(
            username='admin',
            email='admin@dgc.gov.jm',
            first_name='System',
            last_name='Administrator',
            role=Role.ADMIN,
            must_change_password=False,
        )
        admin.set_password('admin123')
        db.session.add(admin)

        # HOD
        hod = User(
            username='hod',
            email='hod@dgc.gov.jm',
            first_name='Head',
            last_name='Department',
            role=Role.HOD,
        )
        hod.set_password('hod123')
        db.session.add(hod)

        # Deputy
        deputy = User(
            username='deputy',
            email='deputy@dgc.gov.jm',
            first_name='Deputy',
            last_name='Director',
            role=Role.DEPUTY,
        )
        deputy.set_password('deputy123')
        db.session.add(deputy)

        # Senior Chemists (one per branch)
        branches = [
            ('sc_tox', 'Senior', 'Chemist-Tox', Branch.TOXICOLOGY),
            ('sc_pharma', 'Senior', 'Chemist-Pharma', Branch.PHARMACEUTICAL),
            ('sc_milk', 'Senior', 'Chemist-Milk', Branch.FOOD_MILK),
            ('sc_alcohol', 'Senior', 'Chemist-Alcohol', Branch.FOOD_ALCOHOL),
        ]
        for uname, fname, lname, branch in branches:
            sc = User(
                username=uname,
                email=f'{uname}@dgc.gov.jm',
                first_name=fname,
                last_name=lname,
                role=Role.SENIOR_CHEMIST,
                branch=branch,
            )
            sc.set_password('senior123')
            db.session.add(sc)

        # Chemists (two per branch)
        chemists = [
            ('chem_tox1', 'Alice', 'Tox', Branch.TOXICOLOGY),
            ('chem_tox2', 'Bob', 'Tox', Branch.TOXICOLOGY),
            ('chem_pharma1', 'Carol', 'Pharma', Branch.PHARMACEUTICAL),
            ('chem_pharma2', 'Dave', 'Pharma', Branch.PHARMACEUTICAL),
            ('chem_milk1', 'Eve', 'Milk', Branch.FOOD_MILK),
            ('chem_milk2', 'Frank', 'Milk', Branch.FOOD_MILK),
            ('chem_alc1', 'Grace', 'Alcohol', Branch.FOOD_ALCOHOL),
            ('chem_alc2', 'Hank', 'Alcohol', Branch.FOOD_ALCOHOL),
        ]
        for uname, fname, lname, branch in chemists:
            c = User(
                username=uname,
                email=f'{uname}@dgc.gov.jm',
                first_name=fname,
                last_name=lname,
                role=Role.CHEMIST,
                branch=branch,
            )
            c.set_password('chemist123')
            db.session.add(c)

        # Officer
        officer = User(
            username='officer1',
            email='officer1@dgc.gov.jm',
            first_name='Jane',
            last_name='Officer',
            role=Role.OFFICER,
        )
        officer.set_password('officer123')
        db.session.add(officer)

        db.session.commit()
        print('Database seeded successfully!')
        print('\nDemo accounts:')
        print('  admin / admin123      - System Administrator')
        print('  hod / hod123          - Head of Department')
        print('  deputy / deputy123    - Deputy Director')
        print('  sc_tox / senior123    - Senior Chemist (Toxicology)')
        print('  sc_pharma / senior123 - Senior Chemist (Pharmaceutical)')
        print('  sc_milk / senior123   - Senior Chemist (Food Milk)')
        print('  sc_alcohol / senior123- Senior Chemist (Food Alcohol)')
        print('  chem_tox1 / chemist123- Chemist (Toxicology)')
        print('  officer1 / officer123 - Officer')


if __name__ == '__main__':
    seed()
