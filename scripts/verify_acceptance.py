"""Read completed sandbox checkpoints and unpack every layer with real Bandizip."""
import argparse
import hashlib
import json
import shutil
import subprocess
import uuid
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from sortzip_core.nested_pipeline import list_rows


def require(condition,message):
    if not condition: raise ValueError(message)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    parser.add_argument('source',type=Path)
    options = parser.parse_args()
    root,source = options.output.resolve(),options.source.resolve()
    state = json.loads((root/'.sortzip_resume.json').read_text(encoding='utf-8'))
    require(state.get('schema') in (2,3,4,5) and state.get('kind')=='multilayer',
            'expected a multilayer sandbox checkpoint')
    require(state.get('archive_tool','bandizip')=='bandizip','expected a Bandizip task')
    bandizip = shutil.which('bz')
    require(bandizip is not None,'Bandizip bz was not found on PATH')
    scratch = root.parent/('unpack-'+uuid.uuid4().hex[:8])
    scratch.mkdir()
    recovered = {}
    def unpack(identity,files):
        if identity.startswith('source:'):
            original = identity.removeprefix('source:')
            digest = digest_file(files[0])
            require(digest==digest_file(source/original),'source content mismatch: '+original)
            recovered[original] = digest
            return
        record = state['completed'][identity]
        folder = scratch/identity.replace(':','_')
        folder.mkdir()
        require(len(files)==len(record['files']),'incomplete archive volume set')
        for path,item in zip(files,record['files']):
            shutil.copyfile(path,folder/item['canonical'])
        entry = next(item['canonical'] for item in record['files'] if item['shown']==record['entry'])
        target = folder/'extracted'
        subprocess.run([bandizip,'x','-y','-o:'+str(target),str(folder/entry)],
            check=True,capture_output=True,timeout=120)
        for child in record['inputs']:
            if child.startswith('source:'):
                unpack(child,[target/Path(next(iter(state['sources'][child]))).name])
            else:
                unpack(child,[target/item['shown'] for item in state['completed'][child]['files']])
    for identity in state['final_ids']:
        record = state['completed'][identity]
        unpack(identity,[root/record['folder']/item['shown'] for item in record['files']])
    expected = {str(path.relative_to(source)).replace('\\','/') for path in source.rglob('*') if path.is_file()}
    require(set(recovered)==expected,'recovered file set differs from sources')
    lists = list(root.rglob('List.txt'))
    require(lists,'missing file correspondence lists')
    entries = []
    for path in lists:
        entries.extend((row['原文件名'],row['新文件名'],row['所属压缩包名']) for row in list_rows(path))
    expected_entries = []
    for identity in state['final_ids']:
        record = state['completed'][identity]
        archive = str(Path(record['folder'])/record['entry']).replace('\\','/')
        for original in record['original_ids']:
            renamed = Path(next(iter(state['sources']['source:'+original]))).name
            expected_entries.append((original,renamed,archive))
    require(sorted(entries)==sorted(expected_entries),'file correspondence list mismatch')
    rows = len(entries)
    require(rows==len(expected),'file correspondence count mismatch')
    report = dict(status='ok',files=len(recovered),completed_groups=len(state['completed']),
        final_units=len(state['final_ids']),list_rows=rows,unpacked=str(scratch),sha256=recovered)
    (root.parent/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='sha256'},ensure_ascii=True))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print(json.dumps(dict(status='error',error=type(error).__name__,message=str(error)),ensure_ascii=True),file=sys.stderr)
        sys.exit(1)
