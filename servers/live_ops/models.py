"""The supply side of the demand and supply heatmap.

A trip request is already a row (`ride.Trip`), so demand needs no new table.
Supply does: "how many idle drivers were in this 1 km cell over the last
hour" has no history anywhere, because a driver's live position is a Redis key
that expires in minutes.

So a beat task samples it once a minute (`tasks.sample_supply`) and writes
COUNTS PER CELL. A row never names a driver, and a driver's trail cannot be
rebuilt from the table: that is the point of storing it this way rather than
keeping positions and aggregating later.
"""

from django.db import models


class SupplySample(models.Model):
    #: The city the drivers were in, through the zone containing them. A
    #: driver outside every zone is not sampled: nobody's scope covers them.
    city = models.ForeignKey(
        'admin_access.GeoNode', on_delete=models.CASCADE, related_name='supply_samples',
    )
    #: floor(lat / 0.01), floor(lng / 0.01): the cell, as integers.
    cell_row = models.IntegerField()
    cell_col = models.IntegerField()
    #: Idle, online drivers in the cell at `sampled_at`.
    drivers = models.PositiveIntegerField()
    #: Floored to the minute, so a doubled run writes the same rows.
    sampled_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sampled_at', 'city', 'cell_row', 'cell_col'],
                name='liveops_one_sample_per_cell_minute',
            ),
        ]
        indexes = [
            models.Index(fields=['city', 'sampled_at'], name='liveops_supply_city_time'),
        ]

    def __str__(self):
        return f'{self.sampled_at:%Y-%m-%d %H:%M} cell({self.cell_row},{self.cell_col}) x{self.drivers}'
