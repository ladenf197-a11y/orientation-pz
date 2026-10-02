"""Transform validated building footprints into integer PZ tile masks."""

import math
from dataclasses import dataclass

import numpy as np
from shapely import contains_xy
from shapely.affinity import translate
from shapely.geometry import shape, mapping


def _rotate(x, y, angle_radians):
    cosine = math.cos(angle_radians)
    sine = math.sin(angle_radians)
    return x * cosine + y * sine, -x * sine + y * cosine


def _transform_coordinates(value, projection, angle_radians, tile_size, origin_u, origin_v):
    if isinstance(value, (list, tuple)):
        if (
            len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            x, y = projection.forward(value[0], value[1])
            u, v = _rotate(x, y, angle_radians)
            return [(u - origin_u) / tile_size, (origin_v - v) / tile_size, *value[2:]]
        return [
            _transform_coordinates(child, projection, angle_radians, tile_size, origin_u, origin_v)
            for child in value
        ]
    return value


@dataclass(frozen=True)
class GridTileFrame:
    local_grid_id: int
    angle_degrees: float
    tile_size_meters: float
    origin_u_meters: float
    origin_v_meters: float
    width: int
    height: int
    projection: object

    @classmethod
    def from_local_grid(cls, local_grid, tile_size_meters=1.0):
        if not math.isfinite(tile_size_meters) or tile_size_meters <= 0:
            raise ValueError("PZ tile size must be finite and greater than zero")
        angle = math.radians(local_grid.bearing_degrees)
        rotated_domain = [
            _rotate(x, y, angle) for x, y in local_grid.domain_geometry
        ]
        if not rotated_domain:
            raise ValueError("LocalGrid has no domain geometry")
        min_u = min(point[0] for point in rotated_domain)
        max_u = max(point[0] for point in rotated_domain)
        min_v = min(point[1] for point in rotated_domain)
        max_v = max(point[1] for point in rotated_domain)
        origin_u = math.floor(min_u / tile_size_meters) * tile_size_meters
        origin_v = math.ceil(max_v / tile_size_meters) * tile_size_meters
        width = max(1, math.ceil((max_u - origin_u) / tile_size_meters))
        height = max(1, math.ceil((origin_v - min_v) / tile_size_meters))
        return cls(
            local_grid_id=local_grid.grid_id,
            angle_degrees=local_grid.bearing_degrees,
            tile_size_meters=tile_size_meters,
            origin_u_meters=origin_u,
            origin_v_meters=origin_v,
            width=width,
            height=height,
            projection=local_grid.projection,
        )

    def transform_geometry(self, geometry):
        transformed = dict(geometry)
        angle = math.radians(self.angle_degrees)
        if "coordinates" in transformed:
            transformed["coordinates"] = _transform_coordinates(
                transformed["coordinates"],
                self.projection,
                angle,
                self.tile_size_meters,
                self.origin_u_meters,
                self.origin_v_meters,
            )
        if "geometries" in transformed:
            transformed["geometries"] = [
                self.transform_geometry(child) for child in transformed["geometries"]
            ]
        return shape(transformed)

    def world_tile_origin(self):
        angle = math.radians(self.angle_degrees)
        x = self.origin_u_meters * math.cos(angle) - self.origin_v_meters * math.sin(angle)
        y = self.origin_u_meters * math.sin(angle) + self.origin_v_meters * math.cos(angle)
        return self.projection.inverse(x, y)


@dataclass(frozen=True)
class TileFootprint:
    width: int
    height: int
    mask: tuple
    position: tuple
    footprint: dict
    local_grid_angle: float
    tile_size_meters: float

    @property
    def occupied_tiles(self):
        return sum(sum(row) for row in self.mask)

    def to_dict(self):
        return {
            "width": self.width,
            "height": self.height,
            "mask": [list(row) for row in self.mask],
            "position": list(self.position),
            "footprint": self.footprint,
            "local_grid_angle": self.local_grid_angle,
            "tile_size_meters": self.tile_size_meters,
            "occupied_tiles": self.occupied_tiles,
        }


def rasterize_footprint(geometry, frame, max_sample_tiles=1_000_000):
    """Rasterize tile centers, crop to occupied cells, and preserve input geometry."""
    tile_geometry = frame.transform_geometry(geometry)
    if (tile_geometry.geom_type not in ("Polygon", "MultiPolygon")
            or tile_geometry.is_empty or not tile_geometry.is_valid):
        return None

    min_x, min_y, max_x, max_y = tile_geometry.bounds
    first_column = math.floor(min_x)
    first_row = math.floor(min_y)
    last_column = math.ceil(max_x) - 1
    last_row = math.ceil(max_y) - 1
    if (last_column - first_column + 1) * (last_row - first_row + 1) > max_sample_tiles:
        raise ValueError("footprint exceeds tile rasterization budget")
    occupied = []
    column_centers = np.arange(first_column, last_column + 1, dtype=float) + 0.5
    for row in range(first_row, last_row + 1):
        for offset in np.flatnonzero(contains_xy(tile_geometry, column_centers, row + 0.5)):
            occupied.append((first_column + int(offset), row))
    if not occupied:
        return None

    min_column = min(column for column, _ in occupied)
    max_column = max(column for column, _ in occupied)
    min_row = min(row for _, row in occupied)
    max_row = max(row for _, row in occupied)
    width = max_column - min_column + 1
    height = max_row - min_row + 1
    occupied_set = set(occupied)
    mask = tuple(
        tuple(int((column + min_column, row + min_row) in occupied_set) for column in range(width))
        for row in range(height)
    )
    local_footprint = translate(tile_geometry, xoff=-min_column, yoff=-min_row)
    return TileFootprint(
        width=width,
        height=height,
        mask=mask,
        position=(min_column, min_row),
        footprint=mapping(local_footprint),
        local_grid_angle=frame.angle_degrees,
        tile_size_meters=frame.tile_size_meters,
    )
