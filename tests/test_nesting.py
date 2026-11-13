import unittest

from sortzip_core.nesting import Layer, MaterializedUnit, Unit, plan_layers, resolve_inputs


class NestingTests(unittest.TestCase):
    def inputs(self, count=4, folder='doc'):
        return [Unit(f'b{i}', folder, (f'b{i}',)) for i in range(1, count+1)]

    def test_three_layers_preserve_whole_units_and_original_identity(self):
        plan = plan_layers(self.inputs(), [Layer(1), Layer(2), Layer(2, suffix='.png')])
        self.assertEqual([sum(t.layer_index == i for t in plan.tasks) for i in range(3)], [4, 2, 1])
        second = next(t for t in plan.tasks if t.layer_index == 1)
        available = {identity: MaterializedUnit((identity+'.001', identity+'.002'), True)
                     for identity in second.input_ids}
        self.assertEqual(resolve_inputs(second, available), tuple(
            path for unit in available.values() for path in unit.files))
        self.assertEqual(plan.tasks[-1].unit.original_ids, ('b1', 'b2', 'b3', 'b4'))

    def test_tail_and_folder_boundaries(self):
        inputs = self.inputs(5) + [Unit('other', 'photo', ('other',))]
        plan = plan_layers(inputs, [Layer(2), Layer(2)])
        self.assertEqual([len(t.input_ids) for t in plan.tasks[:4]], [2, 2, 1, 1])
        self.assertEqual(len(plan.final_ids), 3)
        self.assertEqual(plan.tasks[-1].unit.folder, 'photo')

    def test_arbitrary_depth_and_independent_names(self):
        layers = [Layer(1, name_template=f'level{i}_{{n}}') for i in range(40)]
        plan = plan_layers(self.inputs(1), layers)
        self.assertEqual(len(plan.tasks), 40)
        self.assertEqual(plan.tasks[-1].archive_stem, 'level39_1')
        self.assertEqual(plan.tasks[-1].unit.original_ids, ('b1',))

    def test_second_layer_counts_previous_units_instead_of_original_files(self):
        plan = plan_layers(self.inputs(5), [Layer(2), Layer(2)])
        outer = [t for t in plan.tasks if t.layer_index == 1]
        self.assertEqual([len(t.input_ids) for t in outer], [2, 1])
        self.assertEqual([len(t.unit.original_ids) for t in outer], [4, 1])

    def test_inner_suffix_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '最外层'):
            plan_layers(self.inputs(), [Layer(suffix='.png'), Layer()])

    def test_missing_or_partial_unit_does_not_supply_remaining_volumes(self):
        task = plan_layers(self.inputs(), [Layer(2)]).tasks[0]
        for available in ({}, {'b1': MaterializedUnit(('one',), True)},
            {'b1': MaterializedUnit(('one',), True), 'b2': MaterializedUnit(('two',), False)}):
            with self.subTest(available=available), self.assertRaises(ValueError):
                resolve_inputs(task, available)

    def test_gz_rejects_one_logical_unit_with_multiple_files(self):
        task = plan_layers(self.inputs(1), [Layer(archive_format='gz')]).tasks[0]
        with self.assertRaisesRegex(ValueError, '实体文件'):
            resolve_inputs(task, {'b1': MaterializedUnit(('part1', 'part2'), True)})

    def test_cross_unit_alias_and_duplicate_originals_rejected(self):
        task = plan_layers(self.inputs(2), [Layer(2)]).tasks[0]
        with self.assertRaises(ValueError):
            resolve_inputs(task, {'b1': MaterializedUnit(('same',), True),
                                  'b2': MaterializedUnit(('SAME',), True)})
        with self.assertRaises(ValueError):
            plan_layers([Unit('one', 'doc', ('b1',)), Unit('two', 'doc', ('b1',))], [Layer()])

    def test_invalid_paths_formats_and_name_collisions(self):
        for folder in ('../doc', '/doc', 'doc//a', 'C:/doc'):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                plan_layers(self.inputs(1, folder), [Layer()])
        for layer in (Layer(0), Layer(True), Layer(archive_format='rar'),
                      Layer(name_template='fixed'), Layer(name_template='../bad')):
            with self.subTest(layer=layer), self.assertRaises(ValueError):
                plan_layers(self.inputs(), [layer])

    def test_password_hidden_from_debug_representation(self):
        layer = Layer(password='secret-123')
        self.assertNotIn('secret-123', repr(plan_layers(self.inputs(1), [layer])))


if __name__ == '__main__':
    unittest.main()
