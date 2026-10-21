#!/usr/bin/env python3
"""Assemble a Windows x64 offline Studio kit from explicit, checksum-pinned inputs.

Never downloads or executes supplied binaries. Native validation remains mandatory.
The build host needs packaging (or pip's bundled packaging) only for wheel metadata.
"""
from __future__ import annotations
import argparse
import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import zipfile

try:
    from packaging.requirements import Requirement
    from packaging.tags import cpython_tags, compatible_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version
    from packaging.specifiers import SpecifierSet
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.tags import cpython_tags, compatible_tags
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    from pip._vendor.packaging.version import Version
    from pip._vendor.packaging.specifiers import SpecifierSet

SKILL_ROOT=Path(__file__).resolve().parents[1]
MAX_MEMBER=512*1024*1024
MAX_ARCHIVE=2*1024**3
FORBIDDEN={'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}

class BuildError(ValueError): pass

def digest(value): return hashlib.sha256(value).hexdigest()

def safe_path(name):
    parts=name.rstrip('/').split('/')
    if (not parts or any(not p or p in {'.','..'} or p.endswith(('.',' ')) or p.split('.')[0].upper() in FORBIDDEN
        or any(c in p for c in '\\:<>"|?*') or any(ord(c)<32 for c in p) for p in parts)):
        raise BuildError('不安全或 Windows 不可用的包路径：'+name)
    return '/'.join(parts)

def read_archive(path):
    if path.is_symlink() or not path.is_file(): raise BuildError('离线输入须为普通文件，不能是链接。')
    try:
        with zipfile.ZipFile(path) as archive:
            result={};seen=set();total=0
            for item in archive.infolist():
                key=safe_path(item.filename); folded=key.casefold()
                if folded in seen: raise BuildError('包内重复或大小写冲突路径：'+key)
                seen.add(folded)
                mode=(item.external_attr>>16)&0xffff
                if stat.S_ISLNK(mode) or (item.flag_bits&1): raise BuildError('离线包含链接或加密内容。')
                total+=item.file_size
                if item.file_size>MAX_MEMBER or total>MAX_ARCHIVE: raise BuildError('离线包解压体积超过安全上限。')
                if not item.is_dir(): result[key]=archive.read(item)
            return result
    except (zipfile.BadZipFile, RuntimeError) as exc: raise BuildError('离线 ZIP 无法读取。') from exc

def check_pe(data):
    if len(data)<64 or data[:2]!=b'MZ': raise BuildError('Python 可执行文件不是 PE 格式。')
    offset=struct.unpack_from('<I',data,0x3c)[0]
    if offset+6>len(data) or data[offset:offset+4]!=b'PE\0\0' or struct.unpack_from('<H',data,offset+4)[0]!=0x8664:
        raise BuildError('仅支持 Windows x64（AMD64）Python。')

def python_archive(path, version, expected_sha256):
    if not re.fullmatch(r'3\.(10|11|12|13)\.\d+',version): raise BuildError('Python 须为明确的 3.10–3.13 补丁版本。')
    if not re.fullmatch(r'[a-fA-F0-9]{64}',expected_sha256 or ''): raise BuildError('须提供可信离线来源的 Python ZIP SHA256。')
    if path.name!=f'python-{version}-embed-amd64.zip': raise BuildError('解释器文件名与指定 Python 版本/架构不符。')
    if path.is_symlink() or not path.is_file(): raise BuildError('Python ZIP 须为普通文件，不能是链接。')
    if digest(path.read_bytes())!=expected_sha256.lower(): raise BuildError('Python ZIP SHA256 不一致。')
    files=read_archive(path); suffix=''.join(version.split('.')[:2])
    for name in ('python.exe',f'python{suffix}.dll',f'python{suffix}.zip',f'python{suffix}._pth'):
        if name not in files: raise BuildError('嵌入式 Python 缺少 '+name)
    check_pe(files['python.exe']);check_pe(files[f'python{suffix}.dll'])
    return files

def wheel_info(path, version):
    try: name, parsed_version, _, tags=parse_wheel_filename(path.name)
    except Exception as exc: raise BuildError('wheel 文件名无效：'+path.name) from exc
    pair=tuple(int(x) for x in version.split('.')[:2]);abi='cp'+''.join(map(str,pair))
    accepted=set(cpython_tags(pair,abis=[abi],platforms=['win_amd64']))|set(compatible_tags(pair,interpreter=abi,platforms=['win_amd64']))
    if not tags&accepted: raise BuildError('wheel 与目标 Windows/Python 不兼容：'+path.name)
    files=read_archive(path)
    metadata=[key for key in files if key.endswith('.dist-info/METADATA')]
    if len(metadata)!=1: raise BuildError('wheel 必须只有一个 METADATA。')
    prefix=metadata[0].rsplit('/',1)[0];record=prefix+'/RECORD'
    if record not in files or prefix+'/WHEEL' not in files: raise BuildError('wheel 缺少 RECORD 或 WHEEL。')
    meta=BytesParser().parsebytes(files[metadata[0]])
    if not meta['Name'] or not meta['Version']: raise BuildError('wheel 元数据缺少包名或版本。')
    if canonicalize_name(meta['Name'])!=name or Version(meta['Version'])!=parsed_version: raise BuildError('wheel 名称/版本与元数据不符。')
    if meta['Requires-Python'] and Version(version) not in SpecifierSet(meta['Requires-Python']): raise BuildError('依赖不支持指定 Python 版本：'+path.name)
    recorded=set()
    for row in csv.reader(io.StringIO(files[record].decode('utf-8'))):
        if len(row)!=3: raise BuildError('wheel RECORD 格式错误。')
        rel, encoded, size=row
        if rel in recorded or rel not in files: raise BuildError('wheel RECORD 路径重复或缺失。')
        recorded.add(rel)
        if rel==record:
            if encoded or size: raise BuildError('RECORD 自身不得伪造哈希。')
            continue
        if not encoded or not size: raise BuildError('wheel RECORD 存在未校验文件：'+rel)
        algorithm,_,value=encoded.partition('=')
        if algorithm not in {'sha256','sha384','sha512'}: raise BuildError('wheel 哈希算法不受支持。')
        actual=base64.urlsafe_b64encode(hashlib.new(algorithm,files[rel]).digest()).rstrip(b'=').decode()
        if actual!=value or int(size)!=len(files[rel]): raise BuildError('wheel 内容校验失败：'+rel)
    if set(files)-recorded: raise BuildError('wheel 含未登记内容。')
    return {'name':name,'version':str(parsed_version),'filename':path.name,'sha256':digest(path.read_bytes()),
            'requires':meta.get_all('Requires-Dist',[]),'files':files}

def select_wheels(wheelhouse, version, requirement_text):
    if wheelhouse.is_symlink() or not wheelhouse.is_dir(): raise BuildError('wheelhouse 须为普通目录。')
    inventory={}
    for path in sorted(wheelhouse.glob('*.whl')):
        info=wheel_info(path,version)
        if info['name'] in inventory: raise BuildError('请提供每个依赖唯一版本的离线 wheelhouse：'+info['name'])
        inventory[info['name']]=info
    env={'implementation_name':'cpython','implementation_version':version,'os_name':'nt','platform_machine':'AMD64',
         'platform_release':'','platform_system':'Windows','platform_version':'','python_full_version':version,
         'platform_python_implementation':'CPython','python_version':'.'.join(version.split('.')[:2]),'sys_platform':'win32','extra':''}
    queue=[]
    try:
        for line in requirement_text.splitlines():
            line=line.split('#',1)[0].strip()
            if line: queue.append((Requirement(line),{''}))
        selected={};extras={}
        while queue:
            requirement,parent_extras=queue.pop(0)
            if requirement.url: raise BuildError('不允许 URL 或源码依赖；仅使用离线 wheel。')
            if requirement.marker and any(k in str(requirement.marker) for k in ('platform_release','platform_version')):
                raise BuildError('依赖要求特定 Windows 版本，须在目标系统另行构建验证：'+str(requirement))
            if requirement.marker and not any(requirement.marker.evaluate({**env,'extra':extra}) for extra in parent_extras): continue
            name=canonicalize_name(requirement.name);wheel=inventory.get(name)
            if not wheel: raise BuildError('缺少目标平台离线依赖：'+str(requirement))
            if Version(wheel['version']) not in requirement.specifier: raise BuildError('离线依赖版本不满足约束：'+str(requirement))
            requested={''}|set(requirement.extras)
            if name in selected and requested<=extras[name]: continue
            extras.setdefault(name,set()).update(requested);selected[name]=wheel
            queue.extend((Requirement(value),set(extras[name])) for value in wheel['requires'])
        return selected
    except BuildError: raise
    except Exception as exc: raise BuildError('依赖元数据或版本约束无法验证：'+str(exc)) from exc


def _put(root,relative,value):
    parts=safe_path(relative).split('/')
    parent=root
    for part in parts:
        if parent.exists() and any(p.name.casefold()==part.casefold() and p.name!=part for p in parent.iterdir()):
            raise BuildError('Windows 文件名大小写冲突：'+relative)
        parent=parent/part
    path=parent
    if path.exists(): raise BuildError('打包文件发生覆盖冲突：'+relative)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)


# Keep the distribution boundary aligned with export_one_contract_skill.py.
# This builder also ships inside the standalone Skill, so it cannot import the
# development repository's exporter. Windows omits non-Windows launchers and
# keeps its existing additional test/cache exclusions.
PRODUCT_IGNORED_PARTS = {'.DS_Store', '__pycache__', 'tests', '.pytest_cache'}
PRODUCT_IGNORED_DIR_PREFIXES = ('.backup-',)
PRODUCT_IGNORED_SUFFIXES = {'.pyc', '.pyo', '.log', '.sqlite', '.db'}
PRODUCT_IGNORED_BASENAMES = {
    'active-manifest.json', 'events.jsonl', 'session-token', 'bootstrap-secret', 'rule-studio.lock',
}
PRODUCT_RETIRED_FILES = {
    'references/contract-routing.md', 'references/priority-clauses.md',
    'references/review-framework.md', 'references/revision-strategy.md',
}


def _include_product_path(relative):
    if (any(part in PRODUCT_IGNORED_PARTS or part.startswith(PRODUCT_IGNORED_DIR_PREFIXES)
            for part in relative.parts)
            or relative.suffix.lower() in PRODUCT_IGNORED_SUFFIXES
            or relative.name in PRODUCT_IGNORED_BASENAMES):
        return False
    if (relative.as_posix() in PRODUCT_RETIRED_FILES
            or relative.as_posix().startswith(('references/contract-types/', 'archive/'))
            or relative.name == 'migration-ledger.csv'):
        raise BuildError('产品仍包含已退役 v1 路径：'+relative.as_posix())
    return True


def _copy_product(source,target):
    # Only product directories; never external data roots, local profiles, QA or VCS.
    directories=('assets','references','scripts','templates','agents')
    for folder in directories:
        for path in sorted((source/folder).rglob('*')):
            relative=path.relative_to(source)
            if not _include_product_path(relative): continue
            if path.is_symlink(): raise BuildError('产品包含链接：'+relative.as_posix())
            if path.is_file(): _put(target,'one-contract/'+relative.as_posix(),path.read_bytes())
    for name in ('SKILL.md','start.ps1','start.cmd'):
        if not (source/name).is_file(): raise BuildError('产品文件缺失：'+name)
        _put(target,'one-contract/'+name,(source/name).read_bytes())
    for name in ('LICENSE.txt','README.md','CHANGELOG.md','config/reviewer_profile.example.json','config/review_memory.example.json'):
        path=source/name
        if path.is_symlink(): raise BuildError('产品包含链接：'+name)
        if path.is_file(): _put(target,'one-contract/'+name,path.read_bytes())


NATIVE_CHECK = r'''import hashlib,json,os,struct,sys
from pathlib import Path
root=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8',errors='replace')
manifest=json.loads((root/'windows-kit-manifest.json').read_text(encoding='utf-8'))
errors=[]
if sys.platform!='win32' or struct.calcsize('P')!=8 or tuple(sys.version_info[:3])!=tuple(map(int,manifest['python_version'].split('.'))):
    errors.append('Windows x64 / Python version mismatch')
for name,expected in manifest['files'].items():
    path=root/name
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        errors.append('File integrity failed: '+name)
if not errors:
    try:
        import defusedxml,lxml,docx,jsonschema,referencing
        import one_contract_rules.public_repository
        one_contract_rules.public_repository.PublicRepository(skill_root=root/'one-contract')
    except Exception as exc: errors.append('Runtime import/check failed: '+type(exc).__name__+': '+str(exc))
result={'native_environment_checked':not errors,'production_accepted':False,'errors':errors,
        'remaining':['Run Studio UI, private Windows ACL, start/stop/restart, offline contract execution, rendering and manual page acceptance.']}
print(json.dumps(result,ensure_ascii=False,indent=2))
(root/'windows-environment-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
sys.exit(1 if errors else 0)
'''


def build(*, python_zip, python_version, python_sha256, wheelhouse, output, skill_root=SKILL_ROOT):
    python_zip=Path(python_zip);wheelhouse=Path(wheelhouse);output=Path(output).absolute();skill_root=Path(skill_root).resolve()
    if os.path.lexists(output): raise BuildError('输出目录已存在；拒绝覆盖。')
    try: output.relative_to(skill_root)
    except ValueError: pass
    else: raise BuildError('输出目录须在产品源目录之外，避免递归打包。')
    pyfiles=python_archive(python_zip,python_version,python_sha256)
    selected=select_wheels(wheelhouse,python_version,(skill_root/'scripts/requirements.txt').read_text(encoding='utf-8'))
    # Validate the complete closure before creating any output.
    output.mkdir(parents=True)
    try:
        suffix=''.join(python_version.split('.')[:2])
        pyfiles[f'python{suffix}._pth']=(f'python{suffix}.zip\n.\nLib/site-packages\n../one-contract\n../one-contract/scripts\nimport site\n').encode()
        for name,value in pyfiles.items(): _put(output,'runtime/'+name,value)
        for wheel in selected.values():
            for name,value in wheel['files'].items():
                parts=name.split('/')
                if parts[0].endswith('.data'):
                    raise BuildError('依赖包含便携运行包尚不支持的 wheel .data 安装方案：'+name)
                _put(output,'runtime/Lib/site-packages/'+name,value)
        _copy_product(skill_root,output)
        _put(output,'verify_windows.py',NATIVE_CHECK.encode('utf-8'))
        launcher="@echo off\r\nchcp 65001 >nul\r\n\"%~dp0runtime\\python.exe\" -u \"%~dp0verify_windows.py\"\r\nif errorlevel 1 exit /b 1\r\npowershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0one-contract\\start.ps1\" -Python \"%~dp0runtime\\python.exe\" %*\r\n"
        _put(output,'Start-Studio.cmd',launcher.encode('ascii'))
        manifest={'format':'one-contract-windows-offline-kit','schema_version':1,'status':'assembled_unverified',
                  'platform':'win_amd64','python_version':python_version,'python_zip_sha256':python_sha256.lower(),
                  'native_windows_verified':False,'production_accepted':False,
                  'dependencies':[{k:v for k,v in w.items() if k!='files'} for w in sorted(selected.values(),key=lambda w:w['name'])],
                  'external_requirements':['Windows x64','PowerShell 5.1 or newer','LibreOffice soffice, Poppler pdfinfo/pdftoppm on PATH, and appropriate Chinese fonts'],
                  'files':{p.relative_to(output).as_posix():digest(p.read_bytes()) for p in sorted(output.rglob('*')) if p.is_file()}}
        (output/'windows-kit-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return manifest
    except Exception:
        shutil.rmtree(output)
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-zip',type=Path,required=True)
    parser.add_argument('--python-version',required=True)
    parser.add_argument('--python-sha256',required=True)
    parser.add_argument('--wheelhouse',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=build(**vars(args))
        print(json.dumps({'output':str(args.output.resolve()),'status':result['status'],'files':len(result['files']),
                          'dependencies':[w['name']+'=='+w['version'] for w in result['dependencies']],
                          'native_windows_verified':False,'production_accepted':False},ensure_ascii=False,indent=2))
        return 0
    except (BuildError,OSError) as exc:
        print('离线构建未完成：'+str(exc),file=sys.stderr);return 2

if __name__=='__main__': raise SystemExit(main())
