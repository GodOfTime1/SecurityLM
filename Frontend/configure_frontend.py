"""Merge demo settings into the local Streamlit config without replacing its theme."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import tomllib
from constants import MAX_UPLOAD_MB

SETTINGS={'server':{'maxUploadSize':MAX_UPLOAD_MB},'browser':{'gatherUsageStats':False},
          'client':{'toolbarMode':'minimal','showSidebarNavigation':False}}


def update_config(text):
    original=tomllib.loads(text)
    expected=deepcopy(original)
    lines=text.splitlines()
    for section,values in SETTINGS.items():
        expected.setdefault(section,{}).update(values)
        for key,value in values.items():
            header=re.compile(r'^\s*\['+re.escape(section)+r'\]\s*(?:#.*)?$')
            start=next((i for i,line in enumerate(lines) if header.match(line)),None)
            if start is None:
                lines.extend(['','['+section+']']);start=len(lines)-1
            end=next((i for i in range(start+1,len(lines)) if re.match(r'^\s*\[',lines[i])),len(lines))
            option=re.compile(r'^\s*'+re.escape(key)+r'\s*=')
            index=next((i for i in range(start+1,end) if option.match(lines[i])),None)
            rendered=('true' if value else 'false') if isinstance(value,bool) else str(value) if isinstance(value,int) else '"'+value+'"'
            replacement=key+' = '+rendered
            if index is None:lines.insert(end,replacement)
            else:
                comment=lines[index].partition('#')[2]
                lines[index]=replacement+('  #'+comment if comment else '')
    result='\n'.join(lines)+'\n'
    if tomllib.loads(result)!=expected:
        raise ValueError('Config has an unusual layout; merge the four demo settings manually. No file was changed.')
    return result


def configure(root):
    path=Path(root)/'.streamlit/config.toml'
    old=path.read_text(encoding='utf-8-sig') if path.exists() else ''
    new=update_config(old)
    if old==new:return path,None
    backup=None
    if path.exists():
        with tempfile.NamedTemporaryFile(prefix='SecurityLM-config-backup-',suffix='.toml',delete=False) as f:
            f.write(path.read_bytes());backup=Path(f.name)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.tmp',delete=False) as f:
        f.write(new.encode());temp=Path(f.name)
    try:temp.replace(path)
    finally:temp.unlink(missing_ok=True)
    return path,backup


if __name__=='__main__':
    path,backup=configure(Path(__file__).resolve().parent)
    print('Configured:',path)
    if backup:print('Previous config backup:',backup)
    print('Theme settings preserved. Restart Streamlit from this Frontend directory.')
