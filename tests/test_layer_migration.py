import unittest

from sortzip_core.layer_migration import migrate_legacy
from sortzip_core.nesting import Unit, plan_layers


class LayerMigrationTests(unittest.TestCase):
    def units(self):
        return [Unit(f'b{i}','doc',(f'b{i}',)) for i in range(1,6)]

    def test_new_profile_has_single_layer(self):
        result = migrate_legacy({})
        self.assertTrue(result.enabled)
        self.assertEqual(len(result.layers),1)

    def test_legacy_single_retains_groups_format_volume_and_outer_suffix(self):
        result = migrate_legacy(dict(first_compress=True,double_compress=False,group_size=2,
            archive_format='7z',enable_volume=False,first_rename_enable=True,
            first_suffix_replace='.png',password='password123',verify_archive=True))
        layer = result.layers[0]
        self.assertEqual((layer.group_size,layer.archive_format,layer.suffix),(2,'7z','.png'))
        self.assertTrue(layer.verify)
        self.assertEqual(layer.password,'password123')
        self.assertEqual(result.password_required,(True,))

    def test_legacy_double_wraps_each_first_group_and_preserves_number_ranges(self):
        result = migrate_legacy(dict(first_compress=True,double_compress=True,group_size=2,
            enable_volume=True,volume='100MB',archive_rename_enable=True,archive_suffix='.zipp'))
        plan = plan_layers(self.units(),result.layers)
        inner = [task for task in plan.tasks if task.layer_index==0]
        outer = [task for task in plan.tasks if task.layer_index==1]
        self.assertEqual([task.archive_stem for task in inner],['1-2-First','3-4-First','5-First'])
        self.assertEqual([task.archive_stem for task in outer],['1-2','3-4','5'])
        self.assertTrue(result.layers[0].enable_volume)
        self.assertFalse(result.layers[1].enable_volume)
        self.assertEqual(result.layers[1].group_size,1)
        self.assertEqual(result.layers[1].suffix,'.zipp')
        self.assertTrue(all(len(task.input_ids)==1 for task in outer))

    def test_compression_disabled_and_missing_secret_marker(self):
        result = migrate_legacy(dict(first_compress=False,double_compress=True))
        self.assertFalse(result.enabled)
        self.assertEqual(len(result.layers),1)
        result = migrate_legacy(dict(remember_pwd=True))
        self.assertEqual(result.layers[0].password,'')
        self.assertEqual(result.password_required,(True,))

    def test_inactive_capabilities_and_unsupported_format(self):
        result = migrate_legacy(dict(archive_format='gz',group_size=4,enable_volume=True,
            password='password123',remember_pwd=True))
        self.assertEqual(result.layers[0].group_size,1)
        self.assertFalse(result.layers[0].enable_volume)
        self.assertEqual(result.layers[0].password,'')
        with self.assertRaises(ValueError):
            migrate_legacy(dict(archive_format='rar'))

    def test_inner_suffix_is_explicitly_reported_and_not_kept(self):
        result = migrate_legacy(dict(double_compress=True,first_compress=True,
            first_rename_enable=True,first_suffix_replace='.png'))
        self.assertIsNone(result.layers[0].suffix)
        self.assertTrue(result.notices)
