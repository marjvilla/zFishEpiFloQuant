# -*- coding: utf-8 -*-
"""Tests for zfquant.manifest -- plane mapping and the override path.

Covers verification item 3 from the plan.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import division, print_function

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfquant import manifest   # noqa: E402


CHANNELS = ["BF", "RFP", "GFP"]
INDICES = {"BF": 1, "RFP": 2, "GFP": 3}


class TestHyperstackLayout(unittest.TestCase):

    def build(self):
        return manifest.Manifest.for_hyperstack(
            image_key=-3, channel_names=CHANNELS, bf_name="BF",
            channel_indices=INDICES, fish_count=4, image_title="stack.tif")

    def test_one_fish_per_frame(self):
        m = self.build()
        plane = m.resolve(3, "GFP").plane
        self.assertEqual(plane.channel, 3)
        self.assertEqual(plane.t, 3)          # fish 3 -> frame 3
        self.assertEqual(plane.z, 1)

    def test_each_fish_gets_a_distinct_frame(self):
        """The legacy tool forgot to advance the frame, so every 'fish' silently
        re-measured frame 1."""
        m = self.build()
        frames = [m.resolve(f, "GFP").plane.t for f in range(1, 5)]
        self.assertEqual(frames, [1, 2, 3, 4])

    def test_fish_along_z(self):
        m = manifest.Manifest.for_hyperstack(
            -3, CHANNELS, "BF", INDICES, fish_count=3, fish_dimension="z")
        plane = m.resolve(2, "RFP").plane
        self.assertEqual((plane.channel, plane.z, plane.t), (2, 2, 1))


class TestFlatStackLayout(unittest.TestCase):

    def build(self, fish_count=4):
        return manifest.Manifest.for_flat_stack(
            image_key=-3, channel_names=CHANNELS, bf_name="BF",
            fish_count=fish_count, image_title="big.tif")

    def test_consecutive_blocks_of_three(self):
        m = self.build()
        self.assertEqual(m.resolve(1, "BF").plane.slice_index, 1)
        self.assertEqual(m.resolve(1, "RFP").plane.slice_index, 2)
        self.assertEqual(m.resolve(1, "GFP").plane.slice_index, 3)
        self.assertEqual(m.resolve(2, "BF").plane.slice_index, 4)
        self.assertEqual(m.resolve(4, "GFP").plane.slice_index, 12)

    def test_custom_channel_order_within_a_block(self):
        m = manifest.Manifest.for_flat_stack(
            -3, CHANNELS, "BF", fish_count=2,
            slice_order=["GFP", "BF", "RFP"])
        self.assertEqual(m.resolve(1, "GFP").plane.slice_index, 1)
        self.assertEqual(m.resolve(1, "BF").plane.slice_index, 2)
        self.assertEqual(m.resolve(2, "GFP").plane.slice_index, 4)

    def test_first_slice_offset(self):
        m = manifest.Manifest.for_flat_stack(
            -3, CHANNELS, "BF", fish_count=2, first_slice=10)
        self.assertEqual(m.resolve(1, "BF").plane.slice_index, 10)
        self.assertEqual(m.resolve(2, "BF").plane.slice_index, 13)

    def test_description_is_readable_before_measuring(self):
        m = self.build()
        self.assertEqual(m.describe(2, "GFP"), "Fish 2 - GFP -> slice 6")


class TestPerWindowLayout(unittest.TestCase):

    def test_every_fish_shares_the_channel_windows(self):
        m = manifest.Manifest.for_per_window(
            channel_images={"BF": -1, "RFP": -2, "GFP": -3},
            channel_names=CHANNELS, bf_name="BF", fish_count=3)
        self.assertEqual(m.resolve(1, "GFP").plane.image_key, -3)
        self.assertEqual(m.resolve(3, "GFP").plane.image_key, -3)
        self.assertEqual(m.resolve(2, "BF").plane.image_key, -1)

    def test_unmapped_channel_resolves_to_nothing(self):
        m = manifest.Manifest.for_per_window(
            channel_images={"BF": -1}, channel_names=CHANNELS, bf_name="BF",
            fish_count=1)
        self.assertIsNone(m.resolve(1, "GFP").plane)
        self.assertIn("not mapped", m.describe(1, "GFP"))


class TestHyperstackPositionPlanes(unittest.TestCase):
    """The per-position function the interactive review flow calls directly,
    one position at a time -- must agree with for_hyperstack's batch output,
    since review and the older batch constructor are now both thin callers of
    the same math."""

    def test_matches_field_shape_of_for_hyperstack(self):
        planes = manifest.hyperstack_position_planes(-3, INDICES, 3)
        self.assertEqual(planes["GFP"].channel, 3)
        self.assertEqual(planes["GFP"].t, 3)
        self.assertEqual(planes["GFP"].z, 1)

    def test_z_dimension_swaps_which_field_varies(self):
        planes = manifest.hyperstack_position_planes(
            -3, INDICES, 2, fish_dimension="z")
        self.assertEqual((planes["RFP"].channel, planes["RFP"].z,
                          planes["RFP"].t), (2, 2, 1))

    def test_channels_differ_only_in_c(self):
        planes = manifest.hyperstack_position_planes(-3, INDICES, 5)
        self.assertEqual(planes["BF"].t, planes["GFP"].t)
        self.assertNotEqual(planes["BF"].channel, planes["GFP"].channel)

    def test_image_title_passthrough(self):
        planes = manifest.hyperstack_position_planes(
            -3, INDICES, 1, image_title="stack.tif")
        self.assertEqual(planes["BF"].image_title, "stack.tif")

    def test_single_channel(self):
        planes = manifest.hyperstack_position_planes(-3, {"BF": 1}, 1)
        self.assertEqual(list(planes.keys()), ["BF"])

    def test_agrees_with_batch_constructor_at_every_position(self):
        """Pins that the incremental (review) and batch (for_hyperstack)
        paths cannot silently diverge."""
        batch = manifest.Manifest.for_hyperstack(
            -3, CHANNELS, "BF", INDICES, fish_count=6, image_title="s.tif")
        for position in range(1, 7):
            incremental = manifest.hyperstack_position_planes(
                -3, INDICES, position, image_title="s.tif")
            for name in CHANNELS:
                self.assertEqual(incremental[name],
                                 batch.resolve(position, name).plane)

    def test_two_fish_sharing_a_position_resolve_to_equal_planes(self):
        """Multi-fish-per-position: two different fish ordinals set_plane'd
        from the SAME position dict must resolve to equal Planes."""
        m = manifest.Manifest(manifest.LAYOUT_HYPERSTACK, CHANNELS, "BF")
        planes = manifest.hyperstack_position_planes(-3, INDICES, 4)
        for fish in (7, 8):
            for name, plane in planes.items():
                m.set_plane(fish, name, plane)
        self.assertEqual(m.resolve(7, "GFP").plane, m.resolve(8, "GFP").plane)


class TestFlatStackPositionPlanes(unittest.TestCase):

    def test_matches_field_shape_of_for_flat_stack(self):
        planes = manifest.flat_stack_position_planes(-3, CHANNELS, 1, 2)
        self.assertEqual(planes["BF"].slice_index, 4)
        self.assertEqual(planes["GFP"].slice_index, 6)

    def test_first_slice_offset(self):
        planes = manifest.flat_stack_position_planes(-3, CHANNELS, 10, 1)
        self.assertEqual(planes["BF"].slice_index, 10)

    def test_custom_order_changes_stride_mapping(self):
        planes = manifest.flat_stack_position_planes(
            -3, ["GFP", "BF", "RFP"], 1, 2)
        self.assertEqual(planes["GFP"].slice_index, 4)
        self.assertEqual(planes["BF"].slice_index, 5)

    def test_single_channel(self):
        planes = manifest.flat_stack_position_planes(-3, ["BF"], 1, 3)
        self.assertEqual(planes["BF"].slice_index, 3)

    def test_does_not_validate_against_a_real_stack_size(self):
        """Purely arithmetic -- the review flow is responsible for not
        offering a position past the end of the real stack."""
        planes = manifest.flat_stack_position_planes(-3, CHANNELS, 1, 9999)
        self.assertEqual(planes["BF"].slice_index, 29995)

    def test_agrees_with_batch_constructor_at_every_position(self):
        batch = manifest.Manifest.for_flat_stack(
            -3, CHANNELS, "BF", fish_count=5, image_title="s.tif")
        for position in range(1, 6):
            incremental = manifest.flat_stack_position_planes(
                -3, CHANNELS, 1, position, image_title="s.tif")
            for name in CHANNELS:
                self.assertEqual(incremental[name],
                                 batch.resolve(position, name).plane)

    def test_two_fish_sharing_a_position_resolve_to_equal_planes(self):
        m = manifest.Manifest(manifest.LAYOUT_FLAT_STACK, CHANNELS, "BF")
        planes = manifest.flat_stack_position_planes(-3, CHANNELS, 1, 2)
        for fish in (1, 2):
            for name, plane in planes.items():
                m.set_plane(fish, name, plane)
        self.assertEqual(m.resolve(1, "BF").plane, m.resolve(2, "BF").plane)

    def test_none_slot_is_skipped_but_still_counts_toward_stride(self):
        """A slice was imaged but isn't a real channel (e.g. RFP, GFP,
        (skip), BF) -- it must not appear in the result, but the stride
        must still be 4 so fish 2 lands on the right slices."""
        order = ["RFP", "GFP", None, "BF"]
        fish1 = manifest.flat_stack_position_planes(-3, order, 1, 1)
        self.assertEqual(sorted(fish1.keys()), ["BF", "GFP", "RFP"])
        self.assertEqual(fish1["RFP"].slice_index, 1)
        self.assertEqual(fish1["GFP"].slice_index, 2)
        self.assertEqual(fish1["BF"].slice_index, 4)

        fish2 = manifest.flat_stack_position_planes(-3, order, 1, 2)
        self.assertEqual(fish2["RFP"].slice_index, 5)
        self.assertEqual(fish2["GFP"].slice_index, 6)
        self.assertEqual(fish2["BF"].slice_index, 8)

    def test_multiple_none_slots_in_one_block(self):
        order = [None, "BF", None]
        fish2 = manifest.flat_stack_position_planes(-3, order, 1, 2)
        self.assertEqual(fish2, {"BF": manifest.Plane(-3, slice_index=5)})


class TestFishCount(unittest.TestCase):

    def test_counts_distinct_fish_with_at_least_one_channel(self):
        m = manifest.Manifest.for_hyperstack(-3, CHANNELS, "BF", INDICES,
                                             fish_count=5)
        self.assertEqual(m.fish_count(), 5)

    def test_empty_manifest_counts_zero(self):
        m = manifest.Manifest(manifest.LAYOUT_PER_WINDOW, CHANNELS, "BF")
        self.assertEqual(m.fish_count(), 0)

    def test_counts_fish_set_one_channel_at_a_time(self):
        m = manifest.Manifest(manifest.LAYOUT_PER_WINDOW, CHANNELS, "BF")
        m.set_plane(1, "BF", manifest.Plane(-1))
        m.set_plane(2, "BF", manifest.Plane(-1))
        m.set_plane(1, "RFP", manifest.Plane(-2))   # fish 1 again, not new
        self.assertEqual(m.fish_count(), 2)

    def test_has_fish(self):
        m = manifest.Manifest(manifest.LAYOUT_PER_WINDOW, CHANNELS, "BF")
        m.set_plane(1, "BF", manifest.Plane(-1))
        self.assertTrue(m.has_fish(1))
        self.assertFalse(m.has_fish(2))


class TestOverrides(unittest.TestCase):
    """Verification item 3: the odd mislabeled or misplaced fish."""

    def build(self):
        return manifest.Manifest.for_flat_stack(
            -3, CHANNELS, "BF", fish_count=6, image_title="big.tif")

    def test_unoverridden_resolution_is_clean(self):
        m = self.build()
        resolution = m.resolve(2, "GFP")
        self.assertFalse(resolution.overridden)
        self.assertEqual(resolution.origin, manifest.ORIGIN_MANIFEST)
        fields = resolution.as_row_fields()
        self.assertEqual(fields["PlaneOverride"], False)
        self.assertEqual(fields["PlaneExpected"], "")

    def test_override_records_expected_and_actual(self):
        m = self.build()
        self.assertEqual(m.resolve(3, "GFP").plane.slice_index, 9)

        m.override_plane(3, "GFP", manifest.Plane(-3, slice_index=42))

        resolution = m.resolve(3, "GFP")
        self.assertTrue(resolution.overridden)
        self.assertEqual(resolution.plane.slice_index, 42)
        self.assertEqual(resolution.expected.slice_index, 9)

        fields = resolution.as_row_fields()
        self.assertEqual(fields["PlaneOverride"], True)
        self.assertEqual(fields["PlaneRecorded"], "slice 42")
        self.assertEqual(fields["PlaneExpected"], "slice 9")

    def test_override_is_scoped_to_one_fish(self):
        m = self.build()
        m.override_plane(3, "GFP", manifest.Plane(-3, slice_index=42))
        self.assertEqual(m.resolve(4, "GFP").plane.slice_index, 12)
        self.assertFalse(m.resolve(4, "GFP").overridden)

    def test_describe_flags_an_override(self):
        m = self.build()
        m.override_plane(3, "GFP", manifest.Plane(-3, slice_index=42))
        self.assertIn("[overridden]", m.describe(3, "GFP"))

    def test_relabel_swaps_both_directions(self):
        """A mislabeled pair must not leave one channel pointing at the other's
        plane."""
        m = self.build()
        rfp_before = m.resolve(2, "RFP").plane.slice_index   # 5
        gfp_before = m.resolve(2, "GFP").plane.slice_index   # 6

        m.relabel(2, "RFP", "GFP")

        self.assertEqual(m.resolve(2, "RFP").plane.slice_index, gfp_before)
        self.assertEqual(m.resolve(2, "GFP").plane.slice_index, rfp_before)
        self.assertEqual(m.resolve(2, "RFP").origin, manifest.ORIGIN_RELABEL)
        self.assertTrue(m.resolve(2, "GFP").overridden)

    def test_relabel_needs_both_channels_mapped(self):
        m = manifest.Manifest.for_per_window(
            {"BF": -1}, CHANNELS, "BF", fish_count=1)
        self.assertRaises(ValueError, m.relabel, 1, "BF", "GFP")

    def test_promote_shifts_later_fish_by_the_same_delta(self):
        """A systematic offset from some point onward -- not one plane for all."""
        m = self.build()
        # Fish 3's GFP is really at 10, not 9: everything after slipped by one.
        m.override_plane(3, "GFP", manifest.Plane(-3, slice_index=10))
        changed = m.promote_override(3, "GFP", through_fish=6)

        self.assertEqual(changed, [4, 5, 6])
        self.assertEqual(m.resolve(4, "GFP").plane.slice_index, 13)  # 12 + 1
        self.assertEqual(m.resolve(5, "GFP").plane.slice_index, 16)
        self.assertEqual(m.resolve(6, "GFP").plane.slice_index, 19)

    def test_promote_leaves_earlier_fish_alone(self):
        m = self.build()
        m.override_plane(3, "GFP", manifest.Plane(-3, slice_index=10))
        m.promote_override(3, "GFP", through_fish=6)
        self.assertEqual(m.resolve(1, "GFP").plane.slice_index, 3)
        self.assertEqual(m.resolve(2, "GFP").plane.slice_index, 6)
        self.assertFalse(m.resolve(2, "GFP").overridden)

    def test_promote_only_touches_the_named_channel(self):
        m = self.build()
        m.override_plane(3, "GFP", manifest.Plane(-3, slice_index=10))
        m.promote_override(3, "GFP", through_fish=6)
        self.assertEqual(m.resolve(4, "RFP").plane.slice_index, 11)
        self.assertFalse(m.resolve(4, "RFP").overridden)

    def test_promote_requires_an_override_to_promote(self):
        m = self.build()
        self.assertRaises(ValueError, m.promote_override, 3, "GFP", 6)

    def test_override_count(self):
        m = self.build()
        self.assertEqual(m.override_count(), 0)
        m.override_plane(1, "GFP", manifest.Plane(-3, slice_index=99))
        m.override_plane(2, "RFP", manifest.Plane(-3, slice_index=98))
        self.assertEqual(m.override_count(), 2)


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zfquant-manifest-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_preserves_planes_and_overrides(self):
        m = manifest.Manifest.for_flat_stack(-3, CHANNELS, "BF", fish_count=3)
        m.override_plane(2, "GFP", manifest.Plane(-3, slice_index=42))
        m.relabel(3, "RFP", "GFP")

        path = m.save(os.path.join(self.tmp, "manifest.json"))
        loaded = manifest.Manifest.load(path)

        self.assertEqual(loaded.layout, manifest.LAYOUT_FLAT_STACK)
        self.assertEqual(loaded.channel_names, CHANNELS)
        self.assertEqual(loaded.bf_name, "BF")
        self.assertEqual(loaded.resolve(1, "BF").plane.slice_index, 1)
        self.assertEqual(loaded.resolve(2, "GFP").plane.slice_index, 42)
        self.assertTrue(loaded.resolve(2, "GFP").overridden)
        self.assertEqual(loaded.resolve(2, "GFP").expected.slice_index, 6)
        self.assertEqual(loaded.resolve(3, "RFP").origin,
                         manifest.ORIGIN_RELABEL)

    def test_saved_file_is_human_editable(self):
        m = manifest.Manifest.for_flat_stack(-3, CHANNELS, "BF", fish_count=2)
        path = m.save(os.path.join(self.tmp, "manifest.json"))
        with open(path) as handle:
            content = handle.read()
        self.assertIn("slice_index", content)
        self.assertIn("\n", content)        # indented, not one long line

    def test_save_leaves_no_temp_file(self):
        m = manifest.Manifest.for_flat_stack(-3, CHANNELS, "BF", fish_count=1)
        path = m.save(os.path.join(self.tmp, "manifest.json"))
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_rejects_unknown_layout(self):
        self.assertRaises(ValueError, manifest.Manifest, "nonsense",
                          CHANNELS, "BF")


class TestPlausibilityCheck(unittest.TestCase):

    def test_fluorescence_plane_labelled_brightfield_warns(self):
        warning = manifest.check_plane_plausible(
            "BF", "BF", mean=200.0, value_max=65535)
        self.assertIsNotNone(warning)
        self.assertIn("looks like fluorescence", warning)

    def test_brightfield_plane_labelled_fluorescence_warns(self):
        warning = manifest.check_plane_plausible(
            "GFP", "BF", mean=30000.0, value_max=65535)
        self.assertIsNotNone(warning)
        self.assertIn("looks like brightfield", warning)

    def test_correct_labelling_is_silent(self):
        self.assertIsNone(manifest.check_plane_plausible(
            "BF", "BF", mean=30000.0, value_max=65535))
        self.assertIsNone(manifest.check_plane_plausible(
            "GFP", "BF", mean=200.0, value_max=65535))

    def test_unknown_range_is_silent_rather_than_guessing(self):
        self.assertIsNone(manifest.check_plane_plausible(
            "GFP", "BF", mean=200.0, value_max=None))


if __name__ == "__main__":
    unittest.main()
