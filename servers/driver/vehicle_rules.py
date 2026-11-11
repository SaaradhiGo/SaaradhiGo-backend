"""A vehicle's identity, checked against the driver's market (10 Nov).

Make and model come from the country's catalogue (`VehicleCatalogueEntry`); anything else is
"Other" (`custom_make_model`) and is reviewed like every vehicle change. The year must be within
the market's maximum vehicle age, and the number plate must fit the country's format and not
belong to another active vehicle.
"""
from base.regions import plate_problem

from . import requirements as req
from .models import Vehicle, VehicleCatalogueEntry
from .plates import normalise_plate, normalised_plate_expression


class VehicleProblem(Exception):
    def __init__(self, code, message, field):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def apply_identity(driver, vehicle, data, vehicle_type):
    """Set brand, model, year and catalogue fields on `vehicle` from `data`; raise VehicleProblem.

    `data` may carry `catalogue_entry` (an id), or `custom: true` with free-text brand and model.
    A client that sends only brand and model (the app before 10 Nov) is matched against the
    catalogue, and is "Other" when nothing matches.
    """
    config = req.for_driver(driver)
    country = config['country']
    entry_id = data.get('catalogue_entry')
    if entry_id not in (None, ''):
        entry = VehicleCatalogueEntry.objects.filter(pk=entry_id, country=country, is_active=True).first()
        if entry is None or (vehicle_type and entry.vehicle_type != vehicle_type):
            raise VehicleProblem('CATALOGUE_ENTRY', 'Choose a make and model from the list.', 'catalogue_entry')
        vehicle.catalogue_entry, vehicle.brand, vehicle.model = entry, entry.make, entry.model
        vehicle.custom_make_model = False
    elif 'brand' in data or 'model' in data or data.get('custom'):
        brand = ' '.join(str(data.get('brand', vehicle.brand) or '').split())[:100]
        model = ' '.join(str(data.get('model', vehicle.model) or '').split())[:100]
        if data.get('custom') and not (brand and model):
            raise VehicleProblem('MAKE_MODEL', 'Enter the make and model.', 'brand')
        match = VehicleCatalogueEntry.objects.filter(
            country=country, vehicle_type=vehicle_type or '', make__iexact=brand, model__iexact=model,
            is_active=True).first() if brand and model else None
        vehicle.brand, vehicle.model = brand, model
        vehicle.catalogue_entry = match
        vehicle.custom_make_model = bool(brand or model) and match is None
    if data.get('year') not in (None, ''):
        try:
            year = int(data.get('year'))
        except (TypeError, ValueError):
            raise VehicleProblem('YEAR', 'Choose the year.', 'year')
        oldest, newest = req.year_range(config)
        if not oldest <= year <= newest:
            raise VehicleProblem('YEAR', f'The vehicle must be from {oldest} or later.', 'year')
        vehicle.year = year
    if 'vehicle_number' in data:
        plate = normalise_plate(data.get('vehicle_number'))
        problem = plate_problem(country, plate)
        if problem:
            raise VehicleProblem('PLATE_FORMAT', problem, 'vehicle_number')
        taken = (Vehicle.objects.annotate(_plate=normalised_plate_expression())
                 .filter(_plate=plate, status='active'))
        if vehicle.pk:
            taken = taken.exclude(pk=vehicle.pk)
        if taken.exists():
            raise VehicleProblem('PLATE_TAKEN', 'That number plate is already registered to another vehicle.',
                                 'vehicle_number')
        vehicle.vehicle_number = plate
