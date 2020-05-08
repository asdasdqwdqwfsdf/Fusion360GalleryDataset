import adsk.core
import adsk.fusion
import traceback
import json
import os
import sys
import time
import math
from pathlib import Path

from . import deserialize
from . import name


class SketchExtrudeImporter():
    def __init__(self, json_data):
        self.app = adsk.core.Application.get()

        if isinstance(json_data, dict):
            self.data = json_data
        else:
            with open(json_data) as f:
                self.data = json.load(f)

        product = self.app.activeProduct
        self.design = adsk.fusion.Design.cast(product)
        # We have need to be in DirectDesign mode to apply sketch transforms
        self.design.designType = adsk.fusion.DesignTypes.DirectDesignType

    def reconstruct(self):
        # Keep track of the sketch profiles
        sketch_profiles = {}
        for timeline_object in self.data["timeline"]:
            entity_uuid = timeline_object["entity"]
            entity_index = timeline_object["index"]
            entity = self.data["entities"][entity_uuid]
            print('Reconstructing', entity["name"])
            if entity["type"] == "Sketch":
                sketch_profile_set = self.reconstruct_sketch(entity)
                if sketch_profile_set:
                    sketch_profiles.update(**sketch_profile_set)

            elif entity["type"] == "ExtrudeFeature":
                self.reconstruct_extrude_feature(entity, sketch_profiles)

    def find_profile(self, profiles, profile_uuid, profile_data):
        # Sketch profiles are automatically generated by Fusion
        # After we have added the curves we have to traverse the profiles
        # to find one with all of the curve uuids from the original
        sorted_curve_uuids = self.get_curve_uuids(profile_data)
        for profile in profiles:
            found_curve_uuids = []
            for loop in profile.profileLoops:
                for curve in loop.profileCurves:
                    sketch_ent = curve.sketchEntity
                    curve_uuid = name.get_uuid(sketch_ent)
                    if curve_uuid is not None:
                        found_curve_uuids.append(curve_uuid)
            sorted_found_curve_uuids = sorted(found_curve_uuids)
            if sorted_found_curve_uuids == sorted_curve_uuids and self.are_profile_properties_identical(profile, profile_data):
                # print("Profile found with curves", sorted_curve_uuids)
                return profile
        print(f"Profile not found: {profile_uuid} with these curve uuids {sorted_curve_uuids}")
        return None

    def are_profile_properties_identical(self, profile, profile_data):
        profile_props = profile.areaProperties(adsk.fusion.CalculationAccuracy.HighCalculationAccuracy)
        tolerance = 0.000000001
        if not math.isclose(profile_props.area, profile_data["properties"]["area"], abs_tol=tolerance):
            return False
        if not math.isclose(profile_props.perimeter, profile_data["properties"]["perimeter"], abs_tol=tolerance):
            return False
        if not math.isclose(profile_props.centroid.x, profile_data["properties"]["centroid"]["x"], abs_tol=tolerance):
            return False
        if not math.isclose(profile_props.centroid.y, profile_data["properties"]["centroid"]["y"], abs_tol=tolerance):
            return False
        if not math.isclose(profile_props.centroid.z, profile_data["properties"]["centroid"]["z"], abs_tol=tolerance):
            return False
        return True

    def get_curve_uuids(self, profile_data):
        loops = profile_data["loops"]
        curve_uuids = []
        for loop in loops:
            profile_curves = loop["profile_curves"]
            for profile_curve in profile_curves:
                curve_uuids.append(profile_curve["curve"])
        return sorted(curve_uuids)

    def reconstruct_sketch(self, sketch_data):
        if "curves" not in sketch_data:
            return None

        sketches = self.design.rootComponent.sketches
        # We set up the sketch on the XY plane and then transform from there
        sketch = sketches.addWithoutEdges(self.design.rootComponent.xYConstructionPlane)
        # Apply the transform from the sketch - we have to be in direct modeling mode (i.e. DirectDesignType)
        matrix = deserialize.matrix3d(sketch_data["transform"])
        sketch.transform = matrix

        # Draw exactly what the user drew and then search for the profiles
        sketch_profiles = self.reconstruct_curves(sketch, sketch_data)
        return sketch_profiles

    def reconstruct_curves(self, sketch, sketch_data):
        print(len(sketch_data["points"]), "points,", len(sketch_data["curves"]), "curves")
        # Turn off sketch compute until we add all the curves
        sketch.isComputeDeferred = True
        self.reconstruct_sketch_curves(sketch, sketch_data["curves"], sketch_data["points"])
        sketch.isComputeDeferred = False

        # If we draw the user curves we have to recover the profiles that Fusion generates
        sketch_profiles = {}
        for profile_uuid, profile_data in sketch_data["profiles"].items():
            # print("Finding profile", profile_data["profile_uuid"])
            sketch_profile = self.find_profile(sketch.profiles, profile_uuid, profile_data)
            sketch_profiles[profile_uuid] = sketch_profile
        return sketch_profiles

    def reconstruct_sketch_curves(self, sketch, curves_data, points_data):
        for curve_uuid, curve in curves_data.items():
            # Don't bother generating construction geometry
            if curve["construction_geom"]:
                continue
            if curve["type"] == "SketchLine":
                self.reconstruct_sketch_line(sketch.sketchCurves.sketchLines, curve, curve_uuid, points_data)
            elif curve["type"] == "SketchArc":
                self.reconstruct_sketch_arc(sketch.sketchCurves.sketchArcs, curve, curve_uuid, points_data)
            elif curve["type"] == "SketchCircle":
                self.reconstruct_sketch_circle(sketch.sketchCurves.sketchCircles, curve, curve_uuid, points_data)
            else:
                print("Unsupported curve type", curve["type"])

    def reconstruct_sketch_line(self, sketch_lines, curve_data, curve_uuid, points_data):
        start_point_uuid = curve_data["start_point"]
        end_point_uuid = curve_data["end_point"]
        start_point = deserialize.point3d(points_data[start_point_uuid])
        end_point = deserialize.point3d(points_data[end_point_uuid])
        line = sketch_lines.addByTwoPoints(start_point, end_point)
        name.set_custom_uuid(line, curve_uuid)

    def reconstruct_sketch_arc(self, sketch_arcs, curve_data, curve_uuid, points_data):
        start_point_uuid = curve_data["start_point"]
        center_point_uuid = curve_data["center_point"]
        start_point = deserialize.point3d(points_data[start_point_uuid])
        center_point = deserialize.point3d(points_data[center_point_uuid])
        sweep_angle = curve_data["end_angle"] - curve_data["start_angle"]
        arc = sketch_arcs.addByCenterStartSweep(center_point, start_point, sweep_angle)
        name.set_custom_uuid(arc, curve_uuid)

    def reconstruct_sketch_circle(self, sketch_circles, curve_data, curve_uuid, points_data):
        center_point_uuid = curve_data["center_point"]
        center_point = deserialize.point3d(points_data[center_point_uuid])
        radius = curve_data["radius"]
        circle = sketch_circles.addByCenterRadius(center_point, radius)
        name.set_custom_uuid(circle, curve_uuid)

    def reconstruct_extrude_feature(self, extrude_data, sketch_profiles):
        extrudes = self.design.rootComponent.features.extrudeFeatures

        # There can be more than one profile, so we create an object collection
        extrude_profiles = adsk.core.ObjectCollection.create()
        for profile in extrude_data["profiles"]:
            profile_uuid = profile["profile"]
            # print('Profile uuid:', profile_uuid)
            extrude_profiles.add(sketch_profiles[profile_uuid])

        # The operation defines if the extrusion becomes a new body
        # a new component or cuts/joins another body (i.e. boolean operation)
        operation = deserialize.feature_operations(extrude_data["operation"])
        extrude_input = extrudes.createInput(extrude_profiles, operation)

        # Simple extrusion in one direction
        if extrude_data["extent_type"] == "OneSideFeatureExtentType":
            self.set_one_side_extrude_input(extrude_input, extrude_data["extent_one"])
        # Extrusion in two directions with different distances
        elif extrude_data["extent_type"] == "TwoSidesFeatureExtentType":
            self.set_two_side_extrude_input(extrude_input, extrude_data["extent_one"], extrude_data["extent_two"])
        # Symmetrical extrusion by the same distance on each side
        elif extrude_data["extent_type"] == "SymmetricFeatureExtentType":
            self.set_symmetric_extrude_input(extrude_input, extrude_data["extent_one"])

        # The start extent is initialized to be the profile plane
        # but we may need to change it to an offset
        # after all other changes
        self.set_start_extent(extrude_input, extrude_data["start_extent"])
        return extrudes.add(extrude_input)

    def set_start_extent(self, extrude_input, start_extent):
        # Only handle the offset case
        # ProfilePlaneStartDefinition is already setup
        # and other cases we don't handle
        if start_extent["type"] == "OffsetStartDefinition":
            offset_distance = adsk.core.ValueInput.createByReal(start_extent["offset"]["value"])
            offset_start_def = adsk.fusion.OffsetStartDefinition.create(offset_distance)
            extrude_input.startExtent = offset_start_def

    def set_one_side_extrude_input(self, extrude_input, extent_one):
        distance = adsk.core.ValueInput.createByReal(extent_one["distance"]["value"])
        extent_distance = adsk.fusion.DistanceExtentDefinition.create(distance)
        taper_angle = adsk.core.ValueInput.createByReal(0)
        if "taper_angle" in extent_one:
            taper_angle = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        extrude_input.setOneSideExtent(extent_distance, adsk.fusion.ExtentDirections.PositiveExtentDirection, taper_angle)

    def set_two_side_extrude_input(self, extrude_input, extent_one, extent_two):
        distance_one = adsk.core.ValueInput.createByReal(extent_one["distance"]["value"])
        distance_two = adsk.core.ValueInput.createByReal(extent_two["distance"]["value"])
        extent_distance_one = adsk.fusion.DistanceExtentDefinition.create(distance_one)
        extent_distance_two = adsk.fusion.DistanceExtentDefinition.create(distance_two)
        taper_angle_one = adsk.core.ValueInput.createByReal(0)
        taper_angle_two = adsk.core.ValueInput.createByReal(0)
        if "taper_angle" in extent_one:
            taper_angle_one = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        if "taper_angle" in extent_two:
            taper_angle_two = adsk.core.ValueInput.createByReal(extent_two["taper_angle"]["value"])
        extrude_input.setTwoSidesExtent(extent_distance_one, extent_distance_two, taper_angle_one, taper_angle_two)

    def set_symmetric_extrude_input(self, extrude_input, extent_one):
        # SYMMETRIC EXTRUDE
        # Symmetric extent is currently buggy when a taper is applied
        # So instead we use a two sided extent with symmetry
        # Note that the distance is not a DistanceExtentDefinition
        # distance = adsk.core.ValueInput.createByReal(extent_one["distance"]["value"])
        # taper_angle = adsk.core.ValueInput.createByReal(0)
        # if "taper_angle" in extent_one:
        #     taper_angle = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        # is_full_length = extent_one["is_full_length"]
        # extrude_input.setSymmetricExtent(distance, is_full_length, taper_angle)
        #
        # TWO SIDED EXTRUDE WORKAROUND
        distance = extent_one["distance"]["value"]
        if extent_one["is_full_length"]:
            distance = distance * 0.5
        distance_one = adsk.core.ValueInput.createByReal(distance)
        distance_two = adsk.core.ValueInput.createByReal(distance)
        extent_distance_one = adsk.fusion.DistanceExtentDefinition.create(distance_one)
        extent_distance_two = adsk.fusion.DistanceExtentDefinition.create(distance_two)
        taper_angle_one = adsk.core.ValueInput.createByReal(0)
        taper_angle_two = adsk.core.ValueInput.createByReal(0)
        if "taper_angle" in extent_one:
            taper_angle_one = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
            taper_angle_two = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        extrude_input.setTwoSidesExtent(extent_distance_one, extent_distance_two, taper_angle_one, taper_angle_two)
