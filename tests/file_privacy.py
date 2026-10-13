"""Portable private-file assertion: POSIX modes versus Windows user-directory ACL."""
import os,subprocess

def assert_private_file(test,path):
    if os.name != "nt":
        test.assertEqual(path.stat().st_mode & 0o777,0o600)
        return
    script = """param([string]$Path)
$acl=Get-Acl -LiteralPath $Path
$public=@('S-1-1-0','S-1-5-11','S-1-5-32-545')
foreach($rule in $acl.Access){
 $sid=$rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
 if(($public -contains $sid) -and $rule.AccessControlType -eq 'Allow'){exit 1}
}
exit 0
"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as directory:
        file=Path(directory)/'check-acl.ps1';file.write_text(script,encoding='utf-8-sig')
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(file),str(path)],capture_output=True,text=True)
        test.assertEqual(result.returncode,0,'File grants access to a broad Windows principal')
