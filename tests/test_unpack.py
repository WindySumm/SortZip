import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile
from datetime import datetime

from sortzip_core import delivery_manifest as delivery, engine, file_journal, unpack
from sortzip_core.archive_tools import detect_tool
from sortzip_core.nesting import Layer


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'source'
        self.source.mkdir()
        self.files = {'中文 原名.txt':os.urandom(2800),'原文件.zip':b'archive as payload'}
        for name,data in self.files.items():
            (self.source/name).write_bytes(data)

    def config(self,tool,exe,layers,*,original=False,manual=False):
        return dict(src=str(self.source),dest=str(self.root/f'compress-{tool}'),
                    custom_names={'.txt':'doc','.zip':'doc'},keep_files=True,first_compress=True,
                    archive_tool=tool,archive_tool_paths={tool:exe},compression_layers=layers,
                    retain_layers=[],delivery_manifest=True,delivery_original_names=original,
                    restore_original_names=manual,naming_rules=[dict(enable=True,match_folder='doc',template='新的 = {n}{ext}')])

    def test_requires_flat_unique_names(self):
        base = dict(schema=1,layer=1,children=[],files=[dict(name='a.txt',size=0,sha256='0'*64,auxiliary=False)])
        delivery.validate_members(base)
        for bad in ('../escape.txt','x/y.txt','x\\y.txt','a:stream','NUL.txt',delivery.MEMBER):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                delivery.validate_members(dict(base,files=[dict(base['files'][0],name=bad)]))
        with self.assertRaises(ValueError):
            delivery.validate_members(dict(base,files=base['files']+[dict(base['files'][0],name='A.txt')]))

    def test_delivery_password_modes_and_metadata_authentication(self):
        from sortzip_core.delivery_passwords import protect, unlock
        base = dict(kind='sortzip-task',schema=1,groups=[])
        passwords = {'0':'原密 码','1':'different password'}
        phrase = 'correct delivery passphrase'
        envelope = protect(base,'encrypted',passwords,phrase)
        sealed = dict(base,passwords=envelope)
        self.assertNotIn(passwords['0'].encode(),delivery.encode(sealed))
        self.assertNotIn(phrase.encode(),delivery.encode(sealed))
        self.assertEqual(unlock(sealed,phrase),passwords)
        with self.assertRaises(ValueError):
            unlock(sealed,'incorrect delivery passphrase')
        with self.assertRaises(ValueError):
            unlock(dict(sealed,schema=2),phrase)
        self.assertEqual(unlock(dict(base,passwords=protect(base,'plain',passwords,'')),''),passwords)
        self.assertEqual(unlock(base,''),{})

    def test_task_manifest_rejects_output_path_conflicts(self):
        def group(stem,target,folder):
            volume = stem+'.zip'
            archive = dict(schema=1,tool='bandizip',format='zip',stem=stem,entry=volume,
                           encrypted=False,suffix=None,volumes=[dict(shown=volume,canonical=volume,
                                                                  size=1,sha256='0'*64)])
            row = dict(name=target,size=0,sha256='0'*64,auxiliary=False,directory=folder)
            return dict(folder='category',layer_count=1,tree=dict(archive=archive,children=[],
                             members=dict(schema=1,layer=1,children=[],files=[row])))

        base = dict(kind='sortzip-task',schema=1)
        first = group('one','docs','')
        delivery.validate_delivery(dict(base,groups=[first,group('two','b.txt','other')]))
        for target,folder in (('b.txt','DOCS'),('docs',''),('b.txt','../exit'),('.sortzip-recovery','')):
            with self.subTest(target=target,folder=folder),self.assertRaises(ValueError):
                delivery.validate_delivery(dict(base,groups=[first,group('two',target,folder)]))

    def test_task_manifest_names_use_category_start_time_and_first_available_suffix(self):
        started = datetime(2026,11,15,14,25)
        choose = delivery.choose_task_manifest_name
        self.assertEqual(choose(self.root,['视频/一','视频/二'],started),'视频-2026-11-15_1425.sortzip.json')
        self.assertEqual(choose(self.root,['视频','文档','音乐'],started),'视频等3类-2026-11-15_1425.sortzip.json')
        long = choose(self.root,['视'*100],started)
        self.assertEqual(long,'视'*40+'-2026-11-15_1425.sortzip.json')
        (self.root/'Docs-2026-11-15_1425.sortzip.json').write_bytes(b'occupied')
        (self.root/'docs-2026-11-15_1425-2.sortzip.json').mkdir()
        self.assertEqual(choose(self.root,['docs','DOCS/one'],started),'docs-2026-11-15_1425-3.sortzip.json')
        self.assertEqual(choose(self.root,['视频','视频等2类-2026-11-15_1425.sortzip.json'],started),
                         '视频等2类-2026-11-15_1425-2.sortzip.json')
        job = '0'*32
        self.assertEqual(delivery.task_manifest_name(dict(job=job)),f'SortZip-{job}.sortzip.json')
        with self.assertRaises(ValueError):
            delivery.task_manifest_name(dict(job=job,task_delivery=True,delivery_manifest_name='../outside.sortzip.json'))

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_readable_manifest_keeps_name_after_interruption_and_conflict(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        config = dict(self.config('bandizip',exe,[Layer(1,'zip')],original=True),managed_recovery=True)
        output = Path(config['dest'])
        stopped = False
        def progress(start,end,done,total,message):
            nonlocal stopped
            stopped = start==40 and done==1
        with patch('sortzip_core.task_recovery.datetime') as clock:
            clock.now.return_value = datetime(2026,11,15,23,59)
            self.assertEqual(engine.main_from_config(config,on_progress=progress,
                    cancel_check=lambda:stopped),'safe_cancelled')
        checkpoint = engine._checkpoint_path(output)
        state = json.loads(checkpoint.read_bytes())
        name = 'doc-2026-11-15_2359.sortzip.json'
        self.assertEqual(state['delivery_manifest_name'],name)
        manifest = output/name
        manifest.write_bytes(b'keep this file')
        resume = dict(resume_from=str(checkpoint),archive_tool_paths={'bandizip':exe})
        with patch('sortzip_core.task_recovery.datetime') as clock:
            clock.now.return_value = datetime(2026,11,16,0,1)
            stats = {}
            self.assertEqual(engine.main_from_config(resume,stats=stats),'error')
            self.assertIn(name,stats['error_detail'])
            self.assertEqual(manifest.read_bytes(),b'keep this file')
            self.assertEqual(json.loads(checkpoint.read_bytes())['delivery_manifest_name'],name)
            manifest.unlink()
            self.assertEqual(engine.main_from_config(resume),'completed')
            clock.now.assert_not_called()
        for verify_name in (name,f"SortZip-{state['job']}.sortzip.json"):
            target_manifest = output/verify_name
            if target_manifest!=manifest:
                manifest.rename(target_manifest)
            received = self.root/('received-'+verify_name)
            self.assertEqual(unpack.run(dict(dest=str(received),unpack_manifests=[str(target_manifest)],
                                           archive_tool_paths={'bandizip':exe}),{}),'completed')
            self.assertEqual({file.name:file.read_bytes() for file in received.iterdir()},self.files)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real command-line tools')
    def test_task_manifest_subset_replay_and_keeps_conflicting_files(self):
        for tool,env in (('bandizip','SORTZIP_TEST_BANDIZIP'),('7zip','SORTZIP_TEST_7ZIP')):
            exe = os.environ.get(env) or detect_tool(tool)
            if not exe:
                continue
            with self.subTest(tool=tool):
                child = self.source/'原目录'
                child.mkdir(exist_ok=True)
                for name in self.files:
                    original = self.source/name
                    if original.exists():
                        original.rename(child/name)
                config = self.config(tool,exe,[Layer(1,'zip',password='inner password'),
                                               Layer(1,'7z',True,'1KB',suffix='.data',
                                                     password='outer password')],original=True)
                phrase = 'independent delivery phrase'
                config.update(recursive=True,managed_recovery=True,
                              delivery_password_mode='encrypted',delivery_phrase=phrase)
                self.assertEqual(engine.main_from_config(config),'completed')
                output = Path(config['dest'])
                self.assertFalse((output/file_journal.RECOVERY/file_journal.COMPRESSION).exists())
                manifests = list(output.rglob('*.sortzip.json'))
                self.assertEqual(len(manifests),1)
                path = str(manifests[0])
                manifest = delivery.read(manifests[0],delivery.validate_delivery)
                self.assertEqual(len(manifest['groups']),2)
                first = manifest['groups'][0]
                volume = first['tree']['archive']['volumes'][0]
                self.assertGreater(len(first['tree']['archive']['volumes']),1)
                (output/first['folder']/volume['shown']).rename(output/first['folder']/volume['canonical'])
                row = first['tree']['children'][0]['members']['files'][0]
                self.assertEqual(row['directory'],'原目录')
                received = self.root/f'received-{tool}'
                received.mkdir()
                options = dict(dest=str(received),unpack_manifests=[path],
                               unpack_delivery_phrases={path:phrase},archive_tool_paths={tool:exe},
                               unpack_task_choices={path:[0]},unpack_task_retention={path:{'0':[1]}})
                with file_journal.task_lock(received),self.assertRaisesRegex(ValueError,'正在执行'):
                    unpack.run(options,{})
                self.assertFalse((received/file_journal.RECOVERY).exists())
                target = received/row['directory']/row['original']
                target.parent.mkdir()
                target.write_bytes(b'keep')
                stats = {}
                self.assertEqual(unpack.run(options,stats),'partial')
                self.assertEqual(target.read_bytes(),b'keep')
                recovery = Path(stats['recovery_path'])
                self.assertTrue(recovery.is_relative_to(received/file_journal.RECOVERY))
                self.assertRegex(recovery.name,r'^unpack-\d{4}-\d{2}-\d{2}_\d{6}-')
                self.assertNotIn(phrase,(recovery/'unpack.json').read_text(encoding='utf-8'))
                with patch('sortzip_core.unpack.datetime') as clock:
                    clock.now.return_value = datetime(2027,1,1)
                    replay = {}
                    self.assertEqual(unpack.run(options,replay),'partial')
                    self.assertEqual(replay['recovery_path'],str(recovery))
                target.unlink()
                self.assertEqual(unpack.run(options,{}),'completed')
                self.assertEqual(target.read_bytes(),self.files[row['original']])
                self.assertFalse(recovery.exists())
                self.assertFalse((received/file_journal.RECOVERY).exists())
                other = manifest['groups'][1]['tree']['children'][0]['members']['files'][0]
                self.assertFalse((received/other['directory']/other['original']).exists())
                renamed = dict(options,unpack_task_choices={path:[1]},unpack_task_original={path:{'1':False}})
                self.assertEqual(unpack.run(renamed,{}),'completed')
                self.assertEqual((received/other['directory']/other['name']).read_bytes(),self.files[other['original']])

    def test_group_names_disambiguate_case_and_reserve_archive_folder(self):
        task = unpack.Unpack(dict(dest=str(self.root)),None,None,None,None,{})
        task.work = self.root/'work'
        task.result = task.work/'result'
        task.result.mkdir(parents=True)
        task.options = dict(schema=3,language='en')
        first = task.named_group({'stem':'Lesson'},'1-1',1,create=True)
        first.mkdir()
        second = task.named_group({'stem':'lesson'},'1-2',1,create=True)
        second.mkdir()
        reserved = task.named_group({'stem':'Retained archives'},'1-3',1,create=True)
        self.assertEqual((first.name,second.name,reserved.name),('Lesson','lesson (2)','Retained archives (2)'))
        self.assertEqual(task.named_group({'stem':'lesson'},'1-2',1),second)

    def test_outer_layer_count_and_legacy_publication(self):
        archive = self.root/'sample.zip'
        archive.write_bytes(b'sample')
        base = dict(schema=1,tool='bandizip',format='zip',stem='sample',entry=archive.name,
                    encrypted=False,suffix=None,volumes=[delivery.describe(archive)])
        for count in (0,129,True,'3',None):
            with self.subTest(count=count),self.assertRaises(ValueError):
                delivery.validate_outer(dict(base,schema=2,layer_count=count))
        with self.assertRaises(ValueError):
            delivery.validate_outer(dict(base,layer_count=3))
        path = archive.with_suffix(delivery.EXTENSION)
        delivery.write_new(path,base)
        delivery.write_outer(path,base,3)
        self.assertEqual(delivery.read(path,delivery.validate_outer),base)
        path.unlink()
        delivery.write_outer(path,base,3)
        delivery.write_outer(path,base,3)
        self.assertEqual(delivery.read(path,delivery.validate_outer),dict(base,schema=2,layer_count=3))
        with self.assertRaisesRegex(ValueError,'解包目标已存在'):
            delivery.write_outer(path,base,2)

    def test_password_order_and_unknown_error(self):
        config = dict(dest=str(self.root/'receive'),unpack_passwords=['first','first','second'])
        task = unpack.Unpack(config,None,None,None,lambda *_:'extra',{})
        with patch.object(task,'try_password',side_effect=[False,False,True]) as attempt:
            self.assertEqual(task.password(None,None,{'encrypted':True,'entry':'name'},2),'extra')
            self.assertEqual([item.args[2] for item in attempt.call_args_list],['first','second','extra'])
            self.assertEqual(task.passwords[-1],'extra')
        ambiguous = subprocess.CalledProcessError(2,[],stderr='Data Error in encrypted file. Wrong password?')
        self.assertFalse(unpack._wrong_password('7zip',ambiguous))
        self.assertFalse(unpack._wrong_password('winrar',subprocess.CalledProcessError(3,[])))

    def test_seven_zip_tries_all_candidates_before_showing_ambiguous_warning(self):
        from sortzip_core.archive_tools import ArchiveTool
        entry = self.root/'sample.7z'
        manifest = {'encrypted':True,'entry':entry.name}
        error = subprocess.CalledProcessError(2,[],stderr='ERROR: Data Error in encrypted file. Wrong password? : a.txt')
        tool = ArchiveTool('7zip','7z')
        for results,expected_prompt in (([error,('', '')],False),([error,error,('', '')],True)):
            with self.subTest(prompt=expected_prompt):
                task = unpack.Unpack(dict(dest=str(self.root),unpack_passwords=['first','second']),
                                     None,None,None,lambda *_:'replacement',{})
                with patch.object(engine,'_run_proc',side_effect=results) as process,patch.object(
                        task,'prompt',wraps=task.prompt) as prompt:
                    self.assertEqual(task.password(tool,entry,manifest,2),'replacement' if expected_prompt else 'second')
                    self.assertEqual(process.call_count,3 if expected_prompt else 2)
                    if expected_prompt:
                        prompt.assert_called_once_with(2,entry.name,True)
                        self.assertEqual(task.passwords[-1],'replacement')
                    else:
                        prompt.assert_not_called()
        error.stderr += '\nERROR: Unexpected end of archive'
        with patch.object(engine,'_run_proc',side_effect=error) as process,self.assertRaises(ValueError):
            task.password(tool,entry,manifest,2)
        self.assertEqual(process.call_count,1)

    def test_seven_zip_distinguishes_supported_failure_messages(self):
        entry = self.root/'sample.7z'
        for message,expected in (
            ('ERROR: Wrong password : a.txt','wrong'),
            ('ERROR: CRC Failed in encrypted file. Wrong password? : a.txt','ambiguous'),
            (f'ERROR: {entry}\nCannot open encrypted archive. Wrong password?\n\nERRORS:\nHeaders Error','ambiguous'),
            ('ERROR: CRC Failed : a.txt',None),
            ('ERROR: Missing volume : sample.7z.002',None),
        ):
            with self.subTest(message=message):
                self.assertEqual(unpack._seven_zip_password_failure(
                    subprocess.CalledProcessError(2,[],stderr=message),entry),expected)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_same_named_deliveries_can_be_resumed_individually(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        paths = []
        for index in range(2):
            config = self.config('bandizip',exe,[Layer(2,'zip')],original=bool(index))
            config['dest'] = str(self.root/f'delivery-{index}')
            self.assertEqual(engine.main_from_config(config),'completed')
            paths.append(str(next(Path(config['dest']).rglob('*.sortzip.json'))))
        output = self.root/'received'
        (output/'1-2').mkdir(parents=True)
        (output/'1-2'/'existing.txt').write_text('keep')
        options = dict(dest=str(output),unpack_manifests=paths,archive_tool_paths={'bandizip':exe})
        stats = {}
        def cancelled_after_first():
            return stats['groups']==1
        self.assertEqual(unpack.run(options,stats,cancel=cancelled_after_first),'safe_cancelled')
        self.assertEqual(unpack.run(dict(options,unpack_manifests=paths[:1]),{}),'completed')
        self.assertEqual(unpack.run(dict(options,unpack_manifests=paths[1:]),{}),'completed')
        result = {frozenset(path.name for path in folder.iterdir()) for folder in output.glob('*/1-2')}
        self.assertEqual(result,{frozenset(self.files),frozenset(('新的 = 1.txt','新的 = 2.zip'))})
        self.assertEqual({path.name for path in output.iterdir() if path.is_dir()},
                         {'1-2','1-2 (2)','1-2 (3)'})
        self.assertEqual((output/'1-2/existing.txt').read_text(),'keep')
        output = self.root/'keep-renamed'
        self.assertEqual(unpack.run(dict(options,dest=str(output),unpack_original=False),{}),'completed')
        groups = list(output.glob('*/1-2'))
        self.assertEqual(len(groups),2)
        self.assertTrue(all({path.name for path in group.iterdir()}=={'新的 = 1.txt','新的 = 2.zip'} for group in groups))

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_legacy_checkpoint_preserves_old_folder_names(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        config = self.config('bandizip',exe,[Layer(2,'zip')],original=True)
        self.assertEqual(engine.main_from_config(config),'completed')
        path = next(Path(config['dest']).rglob('*.sortzip.json'))
        manifest = delivery.read(path,delivery.validate_outer)
        key = unpack.sha256(delivery.encode(manifest)).hexdigest()
        label = '1-2-'+key[:12]
        output = self.root/'resume'
        legacy_key = unpack.sha256((key+label).encode()).hexdigest()
        work = output/('.sortzip-unpack-'+legacy_key[:24])
        work.mkdir(parents=True)
        delivery.write_new(work/'unpack.json',dict(schema=2,key=key,result=label,keep=[],original=True))
        self.assertEqual(unpack.run(dict(dest=str(output),unpack_manifests=[str(path)]),{}),'completed')
        self.assertEqual({item.name:item.read_bytes() for item in (output/label/'group-1').iterdir()},self.files)
        self.assertFalse(work.exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_two_groups_use_package_names_in_saved_language(self):
        from sortzip_core import i18n
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        config = self.config('bandizip',exe,[Layer(1,'zip'),Layer(2,'zip')],original=True)
        self.assertEqual(engine.main_from_config(config),'completed')
        path = next(Path(config['dest']).rglob('*.sortzip.json'))
        output = self.root/'receive'
        options = dict(dest=str(output),unpack_manifests=[str(path)],unpack_keep=True)
        with patch.object(i18n,'_language','en'):
            def stop():
                return bool(list(output.glob('.sortzip-unpack-*/group-1-1.json')))
            self.assertEqual(unpack.run(options,{},cancel=stop),'safe_cancelled')
        with patch.object(i18n,'_language','zh'):
            self.assertEqual(unpack.run(options,{}),'completed')
        result = output/'1-2'
        self.assertEqual({folder.name for folder in result.iterdir()},{'1','2','Retained archives'})
        self.assertEqual({file.name:file.read_bytes() for folder in ('1','2') for file in (result/folder).iterdir()},self.files)
        self.assertEqual({item.name for item in (result/'Retained archives'/'Layer 1').iterdir()},{'1','2'})

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real compression tools')
    def test_encrypted_mixed_layers_resume_and_volume_names(self):
        tools = [('bandizip',detect_tool('bandizip'),('zip','7z','zip')),
                 ('7zip',detect_tool('7zip',os.environ.get('SORTZIP_TEST_7ZIP','')),('zip','7z','7z')),
                 ('winrar',detect_tool('winrar',os.environ.get('SORTZIP_TEST_WINRAR','')),('rar','rar','rar'))]
        for tool,exe,formats in tools:
            if not exe:
                continue
            with self.subTest(tool=tool):
                shared = 'outer 1234'
                inner = 'inner 1234'
                layers = [Layer(2,formats[0],password=inner),Layer(1,formats[1],True,'2k',password=inner),
                          Layer(1,formats[2],True,'2k',suffix='.data',password=shared)]
                config = self.config(tool,exe,layers,original=True,manual=True)
                self.assertEqual(engine.main_from_config(config),'completed')
                manifests = list(Path(config['dest']).rglob('*.sortzip.json'))
                self.assertEqual(len(manifests),1)
                manifest = delivery.read(manifests[0],delivery.validate_outer)
                self.assertEqual(manifest['layer_count'],3)
                self.assertGreater(len(manifest['volumes']),1)
                for secret in (inner,shared,*self.files):
                    self.assertNotIn(secret,manifests[0].read_text(encoding='utf-8'))
                originals = {row['shown']:(manifests[0].parent/row['shown']).read_bytes() for row in manifest['volumes']}
                receiver = dict(dest=str(self.root/f'receive-{tool}'),unpack_manifests=list(map(str,manifests)),
                                archive_tool_paths={tool:exe},unpack_retention={str(manifests[0]):[2]},
                                unpack_passwords=[shared])
                def cancelled_after_stage():
                    return bool(list(Path(receiver['dest']).glob('.sortzip-unpack-*/verified-1.json')))
                self.assertEqual(unpack.run(receiver,{},cancel=cancelled_after_stage),'safe_cancelled')
                self.assertFalse((Path(receiver['dest'])/'1').exists())
                receiver.update(unpack_passwords=[shared,inner],unpack_retention={str(manifests[0]):[]})
                code = ('import json,sys; from sortzip_core.unpack import run; '
                        'sys.exit(run(json.loads(sys.argv[1]),{})!="completed")')
                proc = subprocess.run([sys.executable,'-c',code,json.dumps(receiver)],capture_output=True,timeout=40)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                output = Path(receiver['dest'])/'1'
                kept = output/'保留的压缩包'/'第 2 层'/'1'
                self.assertEqual(list((output/'保留的压缩包').iterdir()),[kept.parent])
                self.assertGreater(len(list(kept.iterdir())),1)
                group = output/'1-2'
                self.assertEqual({path.name:path.read_bytes() for path in group.iterdir()},self.files)
                self.assertEqual({name:(manifests[0].parent/name).read_bytes() for name in originals},originals)
                self.assertEqual(unpack.run(receiver,{}),'completed')
                self.assertTrue((output.parent/'1 (2)'/'1-2').is_dir())

                other = manifests[0].with_name('other.sortzip.json')
                delivery.write_new(other,manifest)
                batch = dict(receiver,dest=str(self.root/f'batch-{tool}'),
                             unpack_manifests=[str(manifests[0]),str(other)],
                             unpack_retention={str(manifests[0]):[],str(other):[1,2]})
                self.assertEqual(unpack.run(batch,{}),'completed')
                cleared = Path(batch['dest'])/'1'
                kept_all = Path(batch['dest'])/'other'
                self.assertFalse((cleared/'保留的压缩包').exists())
                self.assertEqual({path.name for path in (kept_all/'保留的压缩包').iterdir()},{'第 1 层','第 2 层'})

                wrong = manifests[0].with_name('wrong.sortzip.json')
                delivery.write_new(wrong,dict(manifest,layer_count=2))
                with self.assertRaisesRegex(ValueError,'声明第 2 层，实际为第 3 层'):
                    unpack.run(dict(receiver,dest=str(self.root/f'wrong-{tool}'),unpack_manifests=[str(wrong)]),{})
                self.assertFalse((self.root/f'wrong-{tool}'/'wrong').exists())
                self.assertEqual({name:(manifests[0].parent/name).read_bytes() for name in originals},originals)

                legacy = manifests[0].with_name('legacy.sortzip.json')
                delivery.write_new(legacy,dict({key:value for key,value in manifest.items() if key!='layer_count'},schema=1))
                for keep in (False,True):
                    options = dict(receiver,dest=str(self.root/f'legacy-{tool}-{keep}'),
                                   unpack_manifests=[str(legacy)],unpack_keep=keep)
                    if keep:
                        def cancelled_after_child():
                            return bool(list(Path(options['dest']).glob('.sortzip-unpack-*/group-1-1.json')))
                        self.assertEqual(unpack.run(options,{},cancel=cancelled_after_child),'safe_cancelled')
                        options['unpack_keep'] = False
                    self.assertEqual(unpack.run(options,{}),'completed')
                    result = Path(options['dest'])/'legacy'
                    self.assertEqual((result/'保留的压缩包').is_dir(),keep)
                    if keep:
                        self.assertEqual({path.name for path in (result/'保留的压缩包').iterdir()},
                                         {'第 1 层','第 2 层'})

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_default_keeps_renamed_files_missing_volume_and_bad_hash(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        config = self.config('bandizip',exe,[Layer(2,'zip',True,'2k',suffix='.data')])
        config['dest'] = str(self.source.parent)
        self.assertEqual(engine.main_from_config(config),'completed')
        path = next(Path(config['dest']).rglob('*.sortzip.json'))
        manifest = delivery.read(path,delivery.validate_outer)
        folder = self.root/'received'
        options = dict(dest=str(folder),unpack_manifests=[str(path)],archive_tool_paths={'bandizip':exe})
        volume = path.parent/manifest['volumes'][0]['shown']
        content = volume.read_bytes()
        volume.unlink()
        with self.assertRaisesRegex(ValueError,'缺少交付分卷'):
            unpack.run(options,{})
        volume.write_bytes(b'!'*len(content))
        with self.assertRaisesRegex(ValueError,'解包文件校验失败'):
            unpack.run(options,{})
        volume.write_bytes(content)
        changed = next(row for row in manifest['volumes'] if row['shown']==volume.name)
        standard = path.parent/changed['canonical']
        self.assertNotEqual(standard,volume)
        standard.write_bytes(content)
        with self.assertRaisesRegex(ValueError,'同时存在两种名称'):
            unpack.run(options,{})
        volume.unlink()
        from types import SimpleNamespace
        with patch('sortzip_core.disk_space.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError,'可用'):
                unpack.run(options,{})
        self.assertFalse((folder/'1-2').exists())
        self.assertEqual(unpack.run(options,{}),'completed')
        result = folder/'1-2'
        names = {item.name for item in (result/'1-2').iterdir()}
        self.assertEqual(names,{'新的 = 1.txt','新的 = 2.zip'})
        self.assertFalse((result/'保留的压缩包').exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_unsafe_members_rejected_before_any_extraction(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        for index in range(2):
            archive = self.root/f'unsafe-{index}.zip'
            with zipfile.ZipFile(archive,'w') as output:
                output.writestr(delivery.MEMBER,'{}')
                if index==0:
                    output.writestr('../outside.txt','bad')
                else:
                    info = zipfile.ZipInfo('malicious')
                    info.create_system = 3
                    info.external_attr = 0xa1ff<<16
                    output.writestr(info,'../outside.txt')
            manifest = dict(schema=1,tool='bandizip',format='zip',stem=archive.stem,entry=archive.name,
                            encrypted=False,suffix=None,volumes=[delivery.describe(archive)])
            path = archive.with_suffix(delivery.EXTENSION)
            delivery.write_new(path,manifest)
            calls = []
            original = engine._run_proc
            def recording(command,*args,**kwargs):
                calls.append(command[1])
                return original(command,*args,**kwargs)
            with patch.object(engine,'_run_proc',side_effect=recording),self.assertRaises(ValueError):
                unpack.run(dict(dest=str(self.root/'receive'),unpack_manifests=[str(path)],
                                archive_tool_paths={'bandizip':exe}),{})
            self.assertNotIn('e',calls)
