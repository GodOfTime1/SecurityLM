"""Allowlisted source packaging; never ships local data, envs, bytecode, or nested ZIPs."""
import argparse
from pathlib import Path
import zipfile

ROOT_FILES={'.gitignore','.dockerignore','.env.production.example','Dockerfile','requirements.txt',
            'README.md','ADVISOR_INTEGRATION.md','MODEL_SELECTOR.md','INVITE_ONLY_DEMO.md',
            'BACKEND_FEATURES.md','SECURITY_REVIEW_V2.md','REVIEW_AND_CI.md'}
ROOT_DIRS={'app':{'.py'},'tests':{'.py'},'scripts':{'.py'}}
EXACT={'.github/workflows/backend-ci.yml','fixtures/model-catalog.example.json','fixtures/vpn-guide.md','fixtures/admin-guide.md'}


def package(root,output):
    root=Path(root).resolve()
    output=Path(output).resolve()
    selected=[]
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():continue
        rel=path.relative_to(root)
        if any(p in {'.git','.venv','venv','env','node_modules','__pycache__','runtime','dist','build'} for p in rel.parts):continue
        if any((root/Path(*rel.parts[:i])).is_symlink() for i in range(1,len(rel.parts))):continue
        if (rel.as_posix() in ROOT_FILES or rel.as_posix() in EXACT or
                (len(rel.parts)>1 and rel.parts[0] in ROOT_DIRS and path.suffix in ROOT_DIRS[rel.parts[0]])):
            selected.append((path,rel))
    if not {'app/main.py','requirements.txt'}.issubset({rel.as_posix() for _,rel in selected}):
        raise RuntimeError('Run packaging against the backend root')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for path,rel in selected:z.write(path,Path('backend')/rel)
    return len(selected)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(f'Packaged {package(args.root,args.output)} source files.')
