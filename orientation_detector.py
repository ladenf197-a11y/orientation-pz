"""Annotate GeoJSON line and polygon features with cardinal orientation."""

import argparse
import copy
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from geometry_validation import (
    GridRectificationResult,
    ValidationResult,
    geometry_validation_gate,
    ordered_issues,
    validate_geometry,
    validate_topology,
)
from building_relationships import build_building_road_relationships
from block_detection import detect_city_blocks
from road_network import extract_road_network


EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True)
class ProjectionContext:
    """Local equirectangular projection anchored at a reference latitude."""

    reference_latitude: float
    longitude_scale: float = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "longitude_scale",
            max(abs(math.cos(math.radians(self.reference_latitude))), 1e-6),
        )

    def forward(self, longitude, latitude):
        return (
            math.radians(longitude) * EARTH_RADIUS_METERS * self.longitude_scale,
            math.radians(latitude) * EARTH_RADIUS_METERS,
        )

    def inverse(self, x, y):
        return (
            math.degrees(x / (EARTH_RADIUS_METERS * self.longitude_scale)),
            math.degrees(y / EARTH_RADIUS_METERS),
        )


@dataclass
class GridEvidence:
    feature_count: int
    inlier_count: int
    support_ratio: float
    mean_feature_confidence: float
    mean_residual_degrees: float
    confidence: float


@dataclass(frozen=True)
class TopologyEdge:
    start_node: int
    end_node: int
    feature_index: int
    semantic_class: str
    rectification_tolerance_degrees: float | None


@dataclass
class TopologyGraph:
    nodes: list
    edges: list
    node_by_coordinate: dict
    source_coordinates: dict


@dataclass(frozen=True)
class FeatureBearing:
    feature_index: int
    x: float
    y: float
    bearing_degrees: float
    confidence: float
    domain_key: object


@dataclass
class SpatialAngularGraph:
    observations: dict
    edges: set
    neighbors: dict

    def density_components(self, minimum_features):
        core_nodes = {
            node for node, adjacent in self.neighbors.items()
            if len(adjacent) + 1 >= minimum_features
        }
        core_components = []
        visited = set()
        for seed in sorted(core_nodes):
            if seed in visited:
                continue
            component = set()
            pending = [seed]
            while pending:
                node = pending.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.add(node)
                pending.extend(
                    neighbor for neighbor in self.neighbors[node]
                    if neighbor in core_nodes and neighbor not in visited
                )
            core_components.append(component)

        component_by_node = {}
        for component_index, component in enumerate(core_components):
            for node in component:
                component_by_node[node] = component_index

        border_members = defaultdict(list)
        for node, adjacent in self.neighbors.items():
            if node in core_nodes:
                continue
            adjacent_components = {component_by_node[neighbor]
                                   for neighbor in adjacent if neighbor in core_nodes}
            if len(adjacent_components) == 1:
                border_members[adjacent_components.pop()].append(node)

        clusters = []
        for component_index, component in enumerate(core_components):
            members = component | set(border_members[component_index])
            clusters.append(sorted(members))
        return clusters


@dataclass
class LocalGrid:
    """A detected grid whose bearing is counter-clockwise from east."""

    grid_id: int
    domain_geometry: list
    bearing_degrees: float
    evidence: GridEvidence
    projection: ProjectionContext
    domain_key: object = None
    feature_indices: list = field(default_factory=list)

    def accepts(self, x, y, bearing, angular_tolerance_degrees):
        if bearing is None or _bearing_distance(bearing, self.bearing_degrees) > angular_tolerance_degrees:
            return False
        return _point_in_polygon((x, y), self.domain_geometry)


def _is_position(value):
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    )


def _geometry_lines(geometry):
    if not geometry:
        return

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates", [])

    if geometry_type == "LineString":
        yield coordinates, False
    elif geometry_type == "MultiLineString":
        for line in coordinates:
            yield line, False
    elif geometry_type == "Polygon":
        for ring in coordinates:
            yield ring, True
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates:
            for ring in polygon:
                yield ring, True
    elif geometry_type == "GeometryCollection":
        for child in geometry.get("geometries", []):
            yield from _geometry_lines(child)


def _segments(geometry):
    lines = list(_geometry_lines(geometry))
    positions = [position for line, _ in lines for position in line if _is_position(position)]
    if not positions:
        return [], False

    mean_latitude = sum(position[1] for position in positions) / len(positions)
    projection = ProjectionContext(mean_latitude)
    result = []

    for line, is_polygon in lines:
        points = [position for position in line if _is_position(position)]
        excluded_edges = set()
        if is_polygon and len(points) > 3 and points[0][:2] == points[-1][:2]:
            ring = points[:-1]
            ring_size = len(ring)
            for index, current in enumerate(ring):
                previous = ring[index - 1]
                following = ring[(index + 1) % ring_size]
                incoming = (current[0] - previous[0], current[1] - previous[1])
                outgoing = (following[0] - current[0], following[1] - current[1])
                incoming_length = math.hypot(*incoming)
                outgoing_length = math.hypot(*outgoing)
                if incoming_length and outgoing_length:
                    cosine = (incoming[0] * outgoing[0] + incoming[1] * outgoing[1]) / (
                        incoming_length * outgoing_length
                    )
                    if cosine < -0.95:
                        excluded_edges.update(((index - 1) % ring_size, index))

        edges = zip(points, points[1:])
        if is_polygon and len(points) > 3 and points[0][:2] == points[-1][:2]:
            edges = ((points[index], points[index + 1]) for index in range(len(points) - 1))
        for edge_index, (start, end) in enumerate(edges):
            if edge_index in excluded_edges:
                continue
            if not _is_position(start) or not _is_position(end):
                continue
            start_x, start_y = projection.forward(start[0], start[1])
            end_x, end_y = projection.forward(end[0], end[1])
            delta_x = end_x - start_x
            delta_y = end_y - start_y
            length = math.hypot(delta_x, delta_y)
            if length > 0:
                bearing = math.degrees(math.atan2(delta_y, delta_x)) % 90
                result.append((length, abs(delta_x), abs(delta_y), is_polygon, bearing))

    return result, any(is_polygon for _, is_polygon in lines)


def detect_orientation(geometry):
    """Return a cardinal orientation and confidence for a GeoJSON geometry."""
    segments, is_polygon = _segments(geometry)
    if not segments:
        return "undetermined", None, 0.0

    polygon_axis_lengths = {"horizontal": [], "vertical": []}
    if is_polygon:
        for length, delta_x, delta_y, segment_is_polygon, _ in segments:
            if segment_is_polygon:
                axis = "horizontal" if delta_x >= delta_y else "vertical"
                polygon_axis_lengths[axis].append(length)

    polygon_axis_caps = {}
    for axis, lengths in polygon_axis_lengths.items():
        if lengths:
            ordered_lengths = sorted(lengths)
            polygon_axis_caps[axis] = ordered_lengths[(len(ordered_lengths) - 1) // 2]

    horizontal_support = 0.0
    vertical_support = 0.0
    for length, delta_x, delta_y, segment_is_polygon, _ in segments:
        axis = "horizontal" if delta_x >= delta_y else "vertical"
        cap = polygon_axis_caps.get(axis) if segment_is_polygon else None
        weight = min(length, cap) if cap else length
        horizontal_support += weight * delta_x / length
        vertical_support += weight * delta_y / length

    total_support = horizontal_support + vertical_support
    confidence = abs(horizontal_support - vertical_support) / total_support
    if confidence < 0.15:
        return "undetermined", None, confidence

    if horizontal_support > vertical_support:
        return "horizontal", 0, confidence
    return "vertical", 90, confidence


def _feature_grid_bearing(geometry):
    segments, _ = _segments(geometry)
    if not segments:
        return None

    bearing_x = 0.0
    bearing_y = 0.0
    total_weight = 0.0
    for length, _, _, _, bearing in segments:
        weight = math.sqrt(length)
        angle = math.radians(4 * bearing)
        bearing_x += weight * math.cos(angle)
        bearing_y += weight * math.sin(angle)
        total_weight += weight

    confidence = math.hypot(bearing_x, bearing_y) / total_weight
    if confidence < 0.25:
        return None
    return (math.degrees(math.atan2(bearing_y, bearing_x)) / 4) % 90, confidence


def _bearing_distance(first, second):
    return abs((first - second + 45) % 90 - 45)


def _domain_grid_fit(features, tolerance_degrees):
    feature_bearings = [
        result for feature in features
        if (result := _feature_grid_bearing(feature.get("geometry"))) is not None
    ]
    if not feature_bearings:
        return None, None

    candidates = []
    for candidate, _ in feature_bearings:
        inliers = [
            (bearing, confidence)
            for bearing, confidence in feature_bearings
            if _bearing_distance(bearing, candidate) <= tolerance_degrees
        ]
        candidates.append((sum(confidence for _, confidence in inliers), candidate, inliers))

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_candidate, inliers = candidates[0]
    total_score = sum(confidence for _, confidence in feature_bearings)
    if best_score < total_score * 0.5:
        return None, None
    if len(candidates) > 1 and math.isclose(best_score, candidates[1][0]):
        if _bearing_distance(best_candidate, candidates[1][1]) > tolerance_degrees:
            return None, None

    bearing_x = sum(confidence * math.cos(math.radians(4 * bearing)) for bearing, confidence in inliers)
    bearing_y = sum(confidence * math.sin(math.radians(4 * bearing)) for bearing, confidence in inliers)
    bearing = (math.degrees(math.atan2(bearing_y, bearing_x)) / 4) % 90
    residuals = [(_bearing_distance(sample, bearing), confidence) for sample, confidence in inliers]
    inlier_weight = sum(confidence for _, confidence in residuals)
    mean_residual = sum(residual * confidence for residual, confidence in residuals) / inlier_weight
    support_ratio = len(inliers) / len(feature_bearings)
    mean_confidence = sum(confidence for _, confidence in inliers) / len(inliers)
    consistency = max(0.0, 1 - mean_residual / max(tolerance_degrees, 1e-9))
    evidence = GridEvidence(
        feature_count=len(feature_bearings),
        inlier_count=len(inliers),
        support_ratio=support_ratio,
        mean_feature_confidence=mean_confidence,
        mean_residual_degrees=mean_residual,
        confidence=support_ratio * mean_confidence * consistency,
    )
    return bearing, evidence


def _convex_domain(points, padding):
    points = sorted(set(points))
    if not points:
        return []
    if len(points) == 1:
        x, y = points[0]
        return [(x - padding, y - padding), (x + padding, y - padding),
                (x + padding, y + padding), (x - padding, y + padding), (x - padding, y - padding)]

    def cross(origin, first, second):
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (first[1] - origin[1]) * (second[0] - origin[0])

    lower = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]

    if len(hull) < 3:
        min_x = min(point[0] for point in points) - padding
        max_x = max(point[0] for point in points) + padding
        min_y = min(point[1] for point in points) - padding
        max_y = max(point[1] for point in points) + padding
        return [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y), (min_x, min_y)]

    center_x = sum(point[0] for point in hull) / len(hull)
    center_y = sum(point[1] for point in hull) / len(hull)
    minimum_radius = min(math.hypot(x - center_x, y - center_y) for x, y in hull)
    scale = 1 + padding / max(minimum_radius, 1e-6)
    expanded = [(center_x + (x - center_x) * scale, center_y + (y - center_y) * scale) for x, y in hull]
    return expanded + [expanded[0]]


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    for start, end in zip(polygon, polygon[1:]):
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        cross = (x - start[0]) * delta_y - (y - start[1]) * delta_x
        if abs(cross) <= 1e-6 and min(start[0], end[0]) - 1e-6 <= x <= max(start[0], end[0]) + 1e-6:
            if min(start[1], end[1]) - 1e-6 <= y <= max(start[1], end[1]) + 1e-6:
                return True
        if (start[1] > y) != (end[1] > y):
            intersection_x = delta_x * (y - start[1]) / delta_y + start[0]
            if x < intersection_x:
                inside = not inside
    return inside


def _coordinate_key(position):
    return position[0], position[1]


def _replace_geometry_positions(original, rectified, replacements):
    result = copy.deepcopy(rectified)

    def replace(original_value, rectified_value):
        if _is_position(original_value) and _is_position(rectified_value):
            coordinate = replacements.get(_coordinate_key(original_value))
            if coordinate is None:
                return rectified_value
            return [coordinate[0], coordinate[1], *rectified_value[2:]]
        if isinstance(original_value, list) and isinstance(rectified_value, list):
            return [replace(original_child, rectified_child)
                    for original_child, rectified_child in zip(original_value, rectified_value)]
        return rectified_value

    if original.get("type") == "GeometryCollection":
        result["geometries"] = [
            _replace_geometry_positions(original_child, rectified_child, replacements)
            for original_child, rectified_child in zip(
                original.get("geometries", []), rectified.get("geometries", [])
            )
        ]
    elif "coordinates" in original and "coordinates" in rectified:
        result["coordinates"] = replace(original["coordinates"], rectified["coordinates"])
    return result


def _changed_segment_count(original, rectified):
    changed = 0
    for (original_line, _), (rectified_line, _) in zip(
        _geometry_lines(original), _geometry_lines(rectified)
    ):
        for original_start, original_end, rectified_start, rectified_end in zip(
            original_line, original_line[1:], rectified_line, rectified_line[1:]
        ):
            if not all(_is_position(position) for position in (
                original_start, original_end, rectified_start, rectified_end
            )):
                continue
            start_changed = any(
                abs(original_start[axis] - rectified_start[axis]) > 1e-12 for axis in (0, 1)
            )
            end_changed = any(
                abs(original_end[axis] - rectified_end[axis]) > 1e-12 for axis in (0, 1)
            )
            changed += start_changed or end_changed
    return changed


def _node_shared_vertices(feature_geometries, grid, tolerance_meters):
    lines = []
    node_positions = {}
    feature_node_counts = {index: 0 for index in feature_geometries}
    for feature_index, geometry in feature_geometries.items():
        for line, _ in _geometry_lines(geometry):
            if not all(_is_position(position) for position in line):
                continue
            lines.append((feature_index, line))
            for position in line:
                node_positions.setdefault(_coordinate_key(position), position[:2])

    projected_nodes = {
        key: grid.projection.forward(position[0], position[1])
        for key, position in node_positions.items()
    }
    segment_buckets = defaultdict(list)
    segments = []
    segment_pairs = set()
    cell_size = max(5, 20 * tolerance_meters)
    for feature_index, line in lines:
        for edge_index, (start, end) in enumerate(zip(line, line[1:])):
            start_key = _coordinate_key(start)
            end_key = _coordinate_key(end)
            start_x, start_y = projected_nodes[start_key]
            end_x, end_y = projected_nodes[end_key]
            segment_index = len(segments)
            segments.append((feature_index, line, edge_index, start_key, end_key))
            min_x = math.floor((min(start_x, end_x) - tolerance_meters) / cell_size)
            max_x = math.floor((max(start_x, end_x) + tolerance_meters) / cell_size)
            min_y = math.floor((min(start_y, end_y) - tolerance_meters) / cell_size)
            max_y = math.floor((max(start_y, end_y) + tolerance_meters) / cell_size)
            for cell_x in range(min_x, max_x + 1):
                for cell_y in range(min_y, max_y + 1):
                    bucket = segment_buckets[(cell_x, cell_y)]
                    for candidate in bucket:
                        segment_pairs.add((candidate, segment_index))
                    bucket.append(segment_index)

    insertions = defaultdict(list)
    for first_index, second_index in sorted(segment_pairs):
        _, first_line, first_edge, first_start_key, first_end_key = segments[first_index]
        _, second_line, second_edge, second_start_key, second_end_key = segments[second_index]
        first_x, first_y = projected_nodes[first_start_key]
        first_end_x, first_end_y = projected_nodes[first_end_key]
        second_x, second_y = projected_nodes[second_start_key]
        second_end_x, second_end_y = projected_nodes[second_end_key]
        first_delta_x = first_end_x - first_x
        first_delta_y = first_end_y - first_y
        second_delta_x = second_end_x - second_x
        second_delta_y = second_end_y - second_y
        denominator = first_delta_x * second_delta_y - first_delta_y * second_delta_x
        if abs(denominator) <= 1e-12:
            continue
        offset_x = second_x - first_x
        offset_y = second_y - first_y
        first_parameter = (offset_x * second_delta_y - offset_y * second_delta_x) / denominator
        second_parameter = (offset_x * first_delta_y - offset_y * first_delta_x) / denominator
        if not (1e-10 < first_parameter < 1 - 1e-10 and 1e-10 < second_parameter < 1 - 1e-10):
            continue

        intersection_x = first_x + first_parameter * first_delta_x
        intersection_y = first_y + first_parameter * first_delta_y
        intersection = list(grid.projection.inverse(intersection_x, intersection_y))
        intersection_key = _coordinate_key(intersection)
        node_positions.setdefault(intersection_key, intersection)
        projected_nodes.setdefault(intersection_key, (intersection_x, intersection_y))
        insertions[(id(first_line), first_edge)].append(
            (first_parameter, intersection_key, intersection)
        )
        insertions[(id(second_line), second_edge)].append(
            (second_parameter, intersection_key, intersection)
        )

    for node_key, (point_x, point_y) in projected_nodes.items():
        cell = (math.floor(point_x / cell_size), math.floor(point_y / cell_size))
        for segment_index in set(segment_buckets.get(cell, ())):
            feature_index, line, edge_index, start_key, end_key = segments[segment_index]
            if node_key in (start_key, end_key):
                continue
            start_x, start_y = projected_nodes[start_key]
            end_x, end_y = projected_nodes[end_key]
            delta_x = end_x - start_x
            delta_y = end_y - start_y
            length_squared = delta_x * delta_x + delta_y * delta_y
            if length_squared == 0:
                continue
            parameter = ((point_x - start_x) * delta_x + (point_y - start_y) * delta_y) / length_squared
            if not 0 < parameter < 1:
                continue
            nearest_x = start_x + parameter * delta_x
            nearest_y = start_y + parameter * delta_y
            if math.hypot(point_x - nearest_x, point_y - nearest_y) <= tolerance_meters:
                insertions[(id(line), edge_index)].append((parameter, node_key, node_positions[node_key]))

    for feature_index, line in lines:
        edge_insertions = [
            insertions.get((id(line), edge_index), [])
            for edge_index in range(len(line) - 1)
        ]
        if not any(edge_insertions):
            continue
        expanded = [line[0]]
        for edge_index, (start, end) in enumerate(zip(line, line[1:])):
            inserted_keys = set()
            for _, node_key, position in sorted(edge_insertions[edge_index]):
                if node_key in inserted_keys:
                    continue
                expanded.append(list(position))
                inserted_keys.add(node_key)
                feature_node_counts[feature_index] += 1
            expanded.append(end)
        line[:] = expanded

    return feature_node_counts


def _building_footprint_follows_grid(geometry, grid, tolerance_degrees):
    if not geometry or grid is None or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return False
    rings = [line for line, is_polygon in _geometry_lines(geometry) if is_polygon]
    if not rings or any(
        len(ring) < 4 or not all(_is_position(position) for position in ring)
        or ring[0][:2] != ring[-1][:2]
        for ring in rings
    ):
        return False

    bearing = math.radians(grid.bearing_degrees)
    cosine = math.cos(bearing)
    sine = math.sin(bearing)
    tangent = math.tan(math.radians(tolerance_degrees))
    total_length = 0.0
    aligned_length = 0.0
    axis_support = {"horizontal": 0, "vertical": 0}
    for ring in rings:
        local_points = []
        for position in ring:
            x, y = grid.projection.forward(position[0], position[1])
            local_points.append((x * cosine + y * sine, -x * sine + y * cosine))
        for start, end in zip(local_points, local_points[1:]):
            delta_u = end[0] - start[0]
            delta_v = end[1] - start[1]
            length = math.hypot(delta_u, delta_v)
            if length == 0:
                continue
            total_length += length
            if abs(delta_v) <= abs(delta_u) * tangent:
                aligned_length += length
                axis_support["horizontal"] += 1
            elif abs(delta_u) <= abs(delta_v) * tangent:
                aligned_length += length
                axis_support["vertical"] += 1

    return (
        total_length > 0
        and aligned_length / total_length >= 0.75
        and axis_support["horizontal"] >= 2
        and axis_support["vertical"] >= 2
    )


def _semantic_rectification_policy(properties, requested_tolerance_degrees, geometry=None, grid=None):
    if properties.get("highway") or properties.get("railway"):
        return "transportation", requested_tolerance_degrees
    if properties.get("building") not in (None, "no", "false", False):
        building_tolerance = min(requested_tolerance_degrees, 5)
        if not _building_footprint_follows_grid(geometry, grid, building_tolerance):
            return "building", None
        return "building", building_tolerance
    if properties.get("waterway") or properties.get("water"):
        return "water", None
    if properties.get("natural") in {"water", "coastline", "wetland", "wood", "scrub"}:
        return "natural", None
    return "generic", requested_tolerance_degrees


def _construct_grid_domain(features, member_indices, projection, radius_meters):
    points = [
        _project_position(position, projection)
        for feature_index in member_indices
        for position in _geometry_positions(features[feature_index].get("geometry"))
    ]
    return _convex_domain(points, max(5, min(20, radius_meters * 0.05)))


def _build_topology_graph(feature_geometries, semantic_policies, projection, tolerance_meters):
    coordinate_positions = {}
    feature_lines = []
    for feature_index, geometry in feature_geometries.items():
        for line, _ in _geometry_lines(geometry):
            if not all(_is_position(position) for position in line):
                continue
            coordinate_keys = []
            for position in line:
                key = _coordinate_key(position)
                coordinate_positions.setdefault(key, position[:2])
                coordinate_keys.append(key)
            feature_lines.append((feature_index, coordinate_keys))

    coordinate_keys = list(coordinate_positions)
    projected_positions = [
        projection.forward(*coordinate_positions[key]) for key in coordinate_keys
    ]
    parent = list(range(len(coordinate_keys)))
    if tolerance_meters > 0:
        buckets = defaultdict(list)
        for index, (x, y) in enumerate(projected_positions):
            cell_x = math.floor(x / tolerance_meters)
            cell_y = math.floor(y / tolerance_meters)
            for offset_x in (-1, 0, 1):
                for offset_y in (-1, 0, 1):
                    for candidate in buckets[(cell_x + offset_x, cell_y + offset_y)]:
                        other_x, other_y = projected_positions[candidate]
                        if math.hypot(x - other_x, y - other_y) <= tolerance_meters:
                            _union(parent, index, candidate)
            buckets[(cell_x, cell_y)].append(index)

    root_members = defaultdict(list)
    for index in range(len(coordinate_keys)):
        root_members[_find(parent, index)].append(index)

    nodes = []
    root_to_node = {}
    node_by_coordinate = {}
    for index, key in enumerate(coordinate_keys):
        root = _find(parent, index)
        if root not in root_to_node:
            members = root_members[root]
            node_x = sum(projected_positions[member][0] for member in members) / len(members)
            node_y = sum(projected_positions[member][1] for member in members) / len(members)
            root_to_node[root] = len(nodes)
            nodes.append((node_x, node_y))
        node_by_coordinate[key] = root_to_node[root]

    edges = []
    for feature_index, coordinate_keys_for_line in feature_lines:
        node_ids = [node_by_coordinate[key] for key in coordinate_keys_for_line]
        edges.extend(
            TopologyEdge(
                start_node,
                end_node,
                feature_index,
                semantic_policies[feature_index][0],
                semantic_policies[feature_index][1],
            )
            for start_node, end_node in zip(node_ids, node_ids[1:])
            if start_node != end_node
        )
    return TopologyGraph(nodes, edges, node_by_coordinate, coordinate_positions)


def _rectify_grid_features(features, grid, tolerance_degrees, topology_tolerance_meters):
    feature_geometries = {}
    for feature_index in grid.feature_indices:
        original_geometry = features[feature_index].get("geometry")
        feature_geometries[feature_index] = copy.deepcopy(original_geometry)

    topology_node_counts = _node_shared_vertices(
        feature_geometries, grid, topology_tolerance_meters
    )
    semantic_policies = {
        feature_index: _semantic_rectification_policy(
            features[feature_index].get("properties") or {},
            tolerance_degrees,
            feature_geometries[feature_index],
            grid,
        )
        for feature_index in feature_geometries
    }
    topology_graph = _build_topology_graph(
        feature_geometries, semantic_policies, grid.projection, topology_tolerance_meters
    )

    if not topology_graph.nodes:
        return {index: (geometry, 0, topology_node_counts[index])
            for index, geometry in feature_geometries.items()}

    bearing = math.radians(grid.bearing_degrees)
    cosine = math.cos(bearing)
    sine = math.sin(bearing)
    local_points = []
    for x, y in topology_graph.nodes:
        local_points.append((x * cosine + y * sine, -x * sine + y * cosine))

    u_parent = list(range(len(topology_graph.nodes)))
    v_parent = list(range(len(topology_graph.nodes)))
    for edge in topology_graph.edges:
        if edge.rectification_tolerance_degrees is None:
            continue
        start_index = edge.start_node
        end_index = edge.end_node
        delta_u = local_points[end_index][0] - local_points[start_index][0]
        delta_v = local_points[end_index][1] - local_points[start_index][1]
        tangent = math.tan(math.radians(edge.rectification_tolerance_degrees))
        if abs(delta_v) <= abs(delta_u) * tangent:
            _union(v_parent, start_index, end_index)
        elif abs(delta_u) <= abs(delta_v) * tangent:
            _union(u_parent, start_index, end_index)

    components = ({}, {})
    for index, (u_value, v_value) in enumerate(local_points):
        components[0].setdefault(_find(u_parent, index), []).append(u_value)
        components[1].setdefault(_find(v_parent, index), []).append(v_value)

    replacements = {}
    for key, index in topology_graph.node_by_coordinate.items():
        u_values = components[0][_find(u_parent, index)]
        v_values = components[1][_find(v_parent, index)]
        if len(u_values) == 1 and len(v_values) == 1:
            replacements[key] = tuple(topology_graph.source_coordinates[key])
            continue
        u_value = sum(u_values) / len(u_values)
        v_value = sum(v_values) / len(v_values)
        x = u_value * cosine - v_value * sine
        y = u_value * sine + v_value * cosine
        longitude, latitude = grid.projection.inverse(x, y)
        replacements[key] = (longitude, latitude)

    rectified = {}
    for feature_index, original_geometry in feature_geometries.items():
        new_geometry = _replace_geometry_positions(original_geometry, original_geometry, replacements)
        rectified[feature_index] = (
            new_geometry,
            _changed_segment_count(original_geometry, new_geometry),
            topology_node_counts[feature_index],
        )
    return rectified


def _local_grid_feature(grid):
    ring = [
        list(grid.projection.inverse(x, y))
        for x, y in grid.domain_geometry
    ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "local_grid_id": grid.grid_id,
            "bearing_degrees": round(grid.bearing_degrees, 4),
            "confidence": round(grid.evidence.confidence, 4),
            "evidence": asdict(grid.evidence),
            "assigned_feature_count": len(grid.feature_indices),
        },
    }


def _feature_anchor(geometry):
    positions = list(_geometry_positions(geometry))
    if not positions:
        return None
    return (
        sum(position[0] for position in positions) / len(positions),
        sum(position[1] for position in positions) / len(positions),
    )


def _domain_key(feature, domain_property):
    if domain_property is None:
        return None
    value = (feature.get("properties") or {}).get(domain_property)
    return json.dumps(value, sort_keys=True) if value is not None else None


def _build_spatial_angular_graph(observations, radius_meters, angular_tolerance_degrees):
    buckets = defaultdict(list)
    for node, observation in observations.items():
        bucket = (
            math.floor(observation.x / radius_meters),
            math.floor(observation.y / radius_meters),
            observation.domain_key,
        )
        buckets[bucket].append(node)

    neighbors = {node: set() for node in observations}
    edges = set()
    for node, observation in observations.items():
        cell_x = math.floor(observation.x / radius_meters)
        cell_y = math.floor(observation.y / radius_meters)
        for offset_x in (-1, 0, 1):
            for offset_y in (-1, 0, 1):
                bucket = (cell_x + offset_x, cell_y + offset_y, observation.domain_key)
                for candidate in buckets[bucket]:
                    if candidate <= node:
                        continue
                    other = observations[candidate]
                    if math.hypot(observation.x - other.x, observation.y - other.y) > radius_meters:
                        continue
                    if _bearing_distance(
                        observation.bearing_degrees, other.bearing_degrees
                    ) > angular_tolerance_degrees:
                        continue
                    edges.add((node, candidate))
                    neighbors[node].add(candidate)
                    neighbors[candidate].add(node)
    return SpatialAngularGraph(observations, edges, neighbors)


def _project_position(position, projection):
    return projection.forward(position[0], position[1])


def _discover_local_grids(features, domain_property, angular_tolerance_degrees, radius_meters, minimum_features):
    anchors = [_feature_anchor(feature.get("geometry")) for feature in features]
    bearings = [_feature_grid_bearing(feature.get("geometry")) for feature in features]
    assignments = [None] * len(features)
    known_anchors = [anchor for anchor in anchors if anchor is not None]
    if not known_anchors:
        return [], assignments

    reference_latitude = sum(latitude for _, latitude in known_anchors) / len(known_anchors)
    projection = ProjectionContext(reference_latitude)
    projected = [
        projection.forward(anchor[0], anchor[1]) if anchor is not None else None
        for anchor in anchors
    ]
    domain_keys = [_domain_key(feature, domain_property) for feature in features]
    observations = {
        index: FeatureBearing(
            feature_index=index,
            x=projected[index][0],
            y=projected[index][1],
            bearing_degrees=bearings[index][0],
            confidence=bearings[index][1],
            domain_key=domain_keys[index],
        )
        for index in range(len(features))
        if projected[index] is not None and bearings[index] is not None
    }
    relation_graph = _build_spatial_angular_graph(
        observations, radius_meters, angular_tolerance_degrees
    )
    clusters = relation_graph.density_components(minimum_features)
    grids = []
    for candidates in clusters:
        candidate_features = [features[index] for index in candidates]
        bearing, _ = _domain_grid_fit(candidate_features, angular_tolerance_degrees)
        if bearing is None:
            continue
        members = [
            index for index in candidates
            if _bearing_distance(bearings[index][0], bearing) <= angular_tolerance_degrees
        ]
        if len(members) < minimum_features:
            continue
        bearing, evidence = _domain_grid_fit(
            [features[index] for index in members], angular_tolerance_degrees
        )
        if bearing is None:
            continue

        grid = LocalGrid(
            grid_id=len(grids) + 1,
            domain_geometry=_construct_grid_domain(
                features, members, projection, radius_meters
            ),
            bearing_degrees=bearing,
            evidence=evidence,
            projection=projection,
            domain_key=domain_keys[candidates[0]],
            feature_indices=list(members),
        )
        grids.append(grid)
        for index in members:
            assignments[index] = grid.grid_id - 1

    for index, anchor in enumerate(projected):
        if anchor is None or assignments[index] is not None or bearings[index] is None:
            continue
        matches = [
            grid
            for grid in grids
            if domain_keys[index] == grid.domain_key
            and grid.accepts(
                anchor[0],
                anchor[1],
                bearings[index][0],
                angular_tolerance_degrees,
            )
        ]
        if len(matches) != 1:
            continue
        grid = matches[0]
        grid.feature_indices.append(index)
        assignments[index] = grid.grid_id - 1

    return grids, assignments


def _find(parent, item):
    while parent[item] != item:
        parent[item] = parent[parent[item]]
        item = parent[item]
    return item


def _union(parent, first, second):
    first_root = _find(parent, first)
    second_root = _find(parent, second)
    parent[second_root] = first_root


def _geometry_positions(geometry):
    for line, _ in _geometry_lines(geometry):
        for position in line:
            if _is_position(position):
                yield position


def annotate_collection(
    collection,
    domain_property=None,
    domain_angle_tolerance_degrees=15,
    rectification_angle_tolerance_degrees=15,
    domain_radius_meters=250,
    minimum_domain_features=3,
    topology_tolerance_meters=0.05,
    building_road_adjacency_distance_meters=40,
    minimum_block_area_meters2=100,
    road_boundary_tolerance_meters=1,
    pz_output_directory=None,
    pz_tile_size_meters=1.0,
    pz_furnish=False,
):
    if collection.get("type") != "FeatureCollection" or not isinstance(collection.get("features"), list):
        raise ValueError("Input must be a GeoJSON FeatureCollection")
    if not 0 <= domain_angle_tolerance_degrees < 45:
        raise ValueError("Domain angle tolerance must be between 0 and 45 degrees")
    if not 0 <= rectification_angle_tolerance_degrees < 45:
        raise ValueError("Rectification angle tolerance must be between 0 and 45 degrees")
    if domain_radius_meters <= 0:
        raise ValueError("Domain radius must be greater than zero meters")
    if minimum_domain_features < 2:
        raise ValueError("Minimum domain features must be at least two")
    if topology_tolerance_meters < 0:
        raise ValueError("Topology tolerance must not be negative")
    if building_road_adjacency_distance_meters < 0:
        raise ValueError("Building-road adjacency distance must not be negative")
    if minimum_block_area_meters2 < 0:
        raise ValueError("Minimum block area must not be negative")
    if road_boundary_tolerance_meters < 0:
        raise ValueError("Road boundary tolerance must not be negative")
    if pz_output_directory is not None and (
        not math.isfinite(pz_tile_size_meters) or pz_tile_size_meters <= 0
    ):
        raise ValueError("PZ tile size must be finite and greater than zero")

    grids, assignments = _discover_local_grids(
        collection["features"],
        domain_property,
        domain_angle_tolerance_degrees,
        domain_radius_meters,
        minimum_domain_features,
    )
    grid_rectifications = {}
    for grid in grids:
        grid_rectifications.update(
            _rectify_grid_features(
                collection["features"], grid, rectification_angle_tolerance_degrees,
                topology_tolerance_meters,
            )
        )

    source_geometries = [feature.get("geometry") for feature in collection["features"]]
    candidate_geometries = [
        grid_rectifications.get(index, (geometry, 0, 0))[0]
        for index, geometry in enumerate(source_geometries)
    ]
    all_positions = [
        position for geometry in source_geometries for position in _geometry_positions(geometry)
    ]
    validation_projection = ProjectionContext(
        sum(position[1] for position in all_positions) / len(all_positions)
        if all_positions else 0
    )
    grid_ids = [grids[index].grid_id if index is not None else None for index in assignments]
    candidate_validations = [
        validate_geometry(source, candidate, validation_projection)
        for source, candidate in zip(source_geometries, candidate_geometries)
    ]
    topology_issues = validate_topology(
        source_geometries,
        candidate_geometries,
        grid_ids,
        validation_projection,
        topology_tolerance_meters,
    )
    rollback_grid_ids = {
        grid_ids[index]
        for index, validation in enumerate(candidate_validations)
        if not validation["valid"] and grid_ids[index] is not None
    }
    rollback_grid_ids.update(
        grid_ids[index]
        for index in topology_issues
        if grid_ids[index] is not None
    )
    grid_failure_reasons = defaultdict(set)
    grid_failure_features = defaultdict(set)
    for feature_index, validation in enumerate(candidate_validations):
        grid_id = grid_ids[feature_index]
        if grid_id is not None and not validation["valid"]:
            grid_failure_features[grid_id].add(feature_index)
            grid_failure_reasons[grid_id].update(
                issue["code"] for issue in validation["issues"]
            )
    for feature_index, issues in topology_issues.items():
        grid_id = grid_ids[feature_index]
        if grid_id is not None:
            grid_failure_features[grid_id].add(feature_index)
            grid_failure_reasons[grid_id].update(issue["code"] for issue in issues)

    features = []
    counts = {"horizontal": 0, "vertical": 0, "undetermined": 0}
    modified_features = 0
    snapped_segments = 0
    inserted_topology_nodes = 0
    generation_ready_indices = []
    restored_features = 0
    invalid_source_features = 0
    validation_results = []
    for feature_index, (feature, grid_index) in enumerate(zip(collection["features"], assignments)):
        orientation, degrees, confidence = detect_orientation(feature.get("geometry"))
        grid = grids[grid_index] if grid_index is not None else None
        grid_bearing = grid.bearing_degrees if grid is not None else None
        candidate_geometry, snapped_count, inserted_nodes = grid_rectifications.get(
            feature_index, (feature.get("geometry"), 0, 0)
        )
        rolled_back = grid is not None and grid.grid_id in rollback_grid_ids
        if rolled_back:
            geometry = feature.get("geometry")
            validation = validate_geometry(geometry, geometry, validation_projection)
            validation_status = "restored_source" if validation["valid"] else "invalid_source"
        else:
            geometry, validation, validation_status, gate_candidate_issues = geometry_validation_gate(
                feature.get("geometry"), candidate_geometry, validation_projection
            )
        candidate_issues = candidate_validations[feature_index]["issues"]
        feature_topology_issues = topology_issues.get(feature_index, [])
        if rolled_back:
            restored_features += 1
            snapped_count = 0
            inserted_nodes = 0
        elif validation_status == "restored_source":
            restored_features += 1
            snapped_count = 0
            inserted_nodes = 0
            candidate_issues = gate_candidate_issues
        generation_eligible = validation["valid"]
        issue_details = ordered_issues(
            validation["issues"] + candidate_issues + feature_topology_issues
        )
        reasons = sorted({issue["code"] for issue in issue_details})
        if rolled_back and not reasons:
            reasons = ["validation_failure"]
        report = ValidationResult(
            source_feature_index=feature_index,
            valid=validation["valid"],
            generation_ready=generation_eligible,
            reasons=tuple(reasons),
            repaired=rolled_back or validation_status == "restored_source",
            status=validation_status,
            issue_details=tuple(issue_details),
        )
        validation_results.append(report)
        if generation_eligible:
            generation_ready_indices.append(feature_index)
        else:
            invalid_source_features += 1
        properties = dict(feature.get("properties") or {})
        semantic_class, semantic_tolerance = _semantic_rectification_policy(
            properties,
            rectification_angle_tolerance_degrees,
            feature.get("geometry"),
            grid,
        )
        properties["dominant_orientation"] = orientation
        properties["dominant_orientation_degrees"] = degrees
        properties["orientation_confidence"] = round(confidence, 4)
        properties["grid_bearing_degrees"] = round(grid_bearing, 4) if grid_bearing is not None else None
        properties["local_grid_id"] = grid.grid_id if grid is not None else None
        properties["grid_confidence"] = round(grid.evidence.confidence, 4) if grid is not None else None
        properties["grid_evidence"] = asdict(grid.evidence) if grid is not None else None
        properties["rectification_semantic_class"] = semantic_class
        properties["rectification_angle_tolerance_degrees"] = semantic_tolerance
        properties["geometry_orthogonalized"] = snapped_count > 0 or inserted_nodes > 0
        properties["orthogonalized_segments"] = snapped_count
        properties["topology_nodes_inserted"] = inserted_nodes
        properties["geometry_validation"] = {
            **report.to_dict(),
            "issues": ordered_issues(validation["issues"]),
            "candidate_issues": ordered_issues(candidate_issues),
            "topology_issues": ordered_issues(feature_topology_issues),
            "area_before_m2": validation["area_before_m2"],
            "area_after_m2": validation["area_after_m2"],
            "holes_before": validation["holes_before"],
            "holes_after": validation["holes_after"],
        }
        properties["generation_eligible"] = generation_eligible
        features.append({**feature, "geometry": geometry, "properties": properties})
        counts[orientation] += 1
        modified_features += snapped_count > 0 or inserted_nodes > 0
        snapped_segments += snapped_count
        inserted_topology_nodes += inserted_nodes

    summary_orientation = "undetermined"
    if counts["horizontal"] > counts["vertical"]:
        summary_orientation = "horizontal"
    elif counts["vertical"] > counts["horizontal"]:
        summary_orientation = "vertical"

    grid_reports = [
        GridRectificationResult(
            grid_id=grid.grid_id,
            rectification="rolled_back" if grid.grid_id in rollback_grid_ids else "accepted",
            affected_features=tuple(sorted(grid.feature_indices)) if grid.grid_id in rollback_grid_ids else (),
            reason="validation_failure" if grid.grid_id in rollback_grid_ids else None,
            failure_feature_indices=tuple(sorted(grid_failure_features[grid.grid_id])),
            reasons=tuple(sorted(grid_failure_reasons[grid.grid_id])),
        )
        for grid in sorted(grids, key=lambda item: item.grid_id)
    ]
    validation_report = {
        "valid": all(result.valid for result in validation_results),
        "generation_ready": all(result.generation_ready for result in validation_results),
        "source_feature_index_base": 0,
        "features": [result.to_dict() for result in sorted(
            validation_results, key=lambda result: result.source_feature_index
        )],
        "grid_rectifications": [result.to_dict() for result in grid_reports],
    }
    road_network_graph = extract_road_network(
        features,
        validation_projection,
        source_feature_indices=generation_ready_indices,
        node_tolerance_meters=topology_tolerance_meters,
    )
    building_road_relationships = build_building_road_relationships(
        features,
        road_network_graph,
        validation_projection,
        generation_ready_indices,
        building_road_adjacency_distance_meters,
    )
    serialized_relationships = [relationship.to_dict() for relationship in building_road_relationships]
    for relationship in serialized_relationships:
        feature_index = relationship["source_feature_index"]
        features[feature_index]["properties"]["road_relationship"] = relationship
    city_blocks = detect_city_blocks(
        features,
        road_network_graph,
        validation_projection,
        generation_ready_indices,
        serialized_relationships,
        minimum_area_meters2=minimum_block_area_meters2,
        road_boundary_tolerance_meters=road_boundary_tolerance_meters,
    )
    building_block_ids = defaultdict(list)
    for block in city_blocks:
        for feature_index in block.buildings:
            building_block_ids[feature_index].append(block.block_id)
    for feature_index, block_ids in building_block_ids.items():
        features[feature_index]["properties"]["city_block_ids"] = sorted(block_ids)
    road_network = road_network_graph.to_dict()
    serialized_blocks = [block.to_dict() for block in city_blocks]

    pz_output = {}
    if pz_output_directory is not None:
        from pz_generation import generate_buildings

        generated = generate_buildings(features, generation_ready_indices, grids,
                                       tile_size_meters=pz_tile_size_meters, furnish=pz_furnish)
        generated.write(pz_output_directory)
        pz_output["pz_generation"] = generated.report

    return {
        **collection,
        "features": features,
        "validation_report": validation_report,
        "road_network": road_network,
        "building_road_relationships": serialized_relationships,
        "city_blocks": serialized_blocks,
        "generation_ready_feature_indices": generation_ready_indices,
        "local_grids": [_local_grid_feature(grid) for grid in grids],
        **pz_output,
        "orientation_summary": {
            "dominant_orientation": summary_orientation,
            "feature_counts": counts,
        },
        "orthogonalization_summary": {
            "domain_property": domain_property,
            "domain_angle_tolerance_degrees": domain_angle_tolerance_degrees,
            "rectification_angle_tolerance_degrees": rectification_angle_tolerance_degrees,
            "domain_radius_meters": domain_radius_meters,
            "minimum_domain_features": minimum_domain_features,
            "topology_tolerance_meters": topology_tolerance_meters,
            "building_road_adjacency_distance_meters": building_road_adjacency_distance_meters,
            "domains_detected": len(grids),
            "features_assigned": sum(grid_index is not None for grid_index in assignments),
            "features_modified": modified_features,
            "segments_snapped": snapped_segments,
            "topology_nodes_inserted": inserted_topology_nodes,
        },
        "geometry_validation_summary": {
            "features_total": len(features),
            "features_valid": len(generation_ready_indices),
            "features_generation_eligible": len(generation_ready_indices),
            "features_restored": restored_features,
            "invalid_source_features": invalid_source_features,
            "features_with_topology_issues": len(topology_issues),
            "rectifications_rejected": len(rollback_grid_ids),
        },
        "city_block_summary": {
            "block_count": len(city_blocks),
            "generation_ready_buildings": sum(
                (features[index].get("properties") or {}).get("building") not in (None, "no", "false", False)
                for index in generation_ready_indices
            ),
            "buildings_assigned": len(building_block_ids),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Annotate GeoJSON and orthogonalize geometry within local grid domains.")
    parser.add_argument("input", help="Input GeoJSON FeatureCollection")
    parser.add_argument("output", nargs="?", default="-", help="Output GeoJSON path (default: stdout)")
    parser.add_argument("--domain-property", help="Optional property that prevents merging different domains")
    parser.add_argument("--domain-angle-tolerance-degrees", type=float, default=15, help="Maximum bearing difference when discovering a local grid")
    parser.add_argument("--rectification-angle-tolerance-degrees", "--snap-tolerance", dest="rectification_angle_tolerance_degrees", type=float, default=15, help="Maximum edge deviation to rectify, in degrees")
    parser.add_argument("--domain-radius-meters", type=float, default=250, help="Maximum distance between domain members")
    parser.add_argument("--minimum-domain-features", type=int, default=3, help="Minimum aligned features needed to form a domain")
    parser.add_argument("--topology-tolerance-meters", type=float, default=0.05, help="Maximum source-node offset for edge noding")
    parser.add_argument("--building-road-adjacency-distance-meters", type=float, default=40, help="Maximum distance for reporting adjacent roads per building")
    parser.add_argument("--minimum-block-area-meters2", type=float, default=100, help="Minimum area for a road-enclosed block")
    parser.add_argument("--road-boundary-tolerance-meters", type=float, default=1, help="Distance for associating road edges to block boundaries")
    parser.add_argument("--pz-output-dir", help="Export building TBX files and one WorldEd project per LocalGrid")
    parser.add_argument("--pz-tile-size-meters", type=float, default=1, help="Meters per PZ tile (default: 1)")
    parser.add_argument("--pz-furnish", action="store_true", help="Add a basic chair when space permits")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as source:
        collection = json.load(source)
    result = annotate_collection(
        collection,
        domain_property=args.domain_property,
        domain_angle_tolerance_degrees=args.domain_angle_tolerance_degrees,
        rectification_angle_tolerance_degrees=args.rectification_angle_tolerance_degrees,
        domain_radius_meters=args.domain_radius_meters,
        minimum_domain_features=args.minimum_domain_features,
        topology_tolerance_meters=args.topology_tolerance_meters,
        building_road_adjacency_distance_meters=args.building_road_adjacency_distance_meters,
        minimum_block_area_meters2=args.minimum_block_area_meters2,
        road_boundary_tolerance_meters=args.road_boundary_tolerance_meters,
        pz_output_directory=args.pz_output_dir,
        pz_tile_size_meters=args.pz_tile_size_meters,
        pz_furnish=args.pz_furnish,
    )
    output = json.dumps(result, indent=2) + "\n"

    if args.output == "-":
        sys.stdout.write(output)
    else:
        with open(args.output, "w", encoding="utf-8") as destination:
            destination.write(output)


if __name__ == "__main__":
    main()
