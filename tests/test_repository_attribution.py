"""Canonical author metadata without absorbing other contributors' identities."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]))
LOGIN='Sirius-Peng';EMAIL='54427718+Sirius-Peng@users.noreply.github.com';PROFILE='https://github.com/'+LOGIN
class RepositoryAttributionTests(unittest.TestCase):
    def package(self):return json.loads((ROOT/'desktop/package.json').read_text(encoding='utf-8'))
    def test_author_is_linked_to_verified_account(self):
        self.assertEqual(self.package()['author'],{'name':LOGIN,'email':EMAIL,'url':PROFILE})
    def test_repository_issue_and_homepage_urls_match(self):
        p=self.package();self.assertEqual(p['repository'],{'type':'git','url':'git+https://github.com/Sirius-Peng/wx-bot.git'})
        self.assertEqual(p['bugs']['url'],PROFILE+'/wx-bot/issues');self.assertEqual(p['homepage'],PROFILE+'/wx-bot#readme')
    def test_contributor_metadata_has_profile_links(self):
        self.assertEqual(self.package()['contributors'],[{'name':LOGIN,'url':PROFILE}])
    def mapped(self,contact):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['git','init','-q',d],check=True)
            return subprocess.check_output(['git','-c','mailmap.file='+str(ROOT/'.mailmap'),'check-mailmap',contact],cwd=d,text=True).strip()
    def test_exact_legacy_alias_is_canonical(self):
        self.assertEqual(self.mapped('nh_y <junsi3105657035@outlook.com>'),LOGIN+' <'+EMAIL+'>')
    def test_unrelated_contributor_is_untouched(self):
        self.assertEqual(self.mapped('Other Author <other@example.org>'),'Other Author <other@example.org>')
    def test_readme_and_contributor_display_are_consistent(self):
        for filename in ['README.md','CONTRIBUTORS.md']:
            text=(ROOT/filename).read_text(encoding='utf-8')
            self.assertIn(PROFILE,text);self.assertIn('54427718?v=4',text);self.assertIn('Yuhan Peng',text)
        self.assertNotIn('contrib.rocks',(ROOT/'CONTRIBUTORS.md').read_text(encoding='utf-8'))
    def test_codeowners_routes_to_maintainer(self):
        lines=[x.strip() for x in (ROOT/'.github/CODEOWNERS').read_text(encoding='utf-8').splitlines() if x.strip() and not x.startswith('#')]
        self.assertEqual(lines,['* @Sirius-Peng'])
    def test_contribution_guide_uses_each_contributors_own_identity(self):
        guide=(ROOT/'CONTRIBUTING.md').read_text(encoding='utf-8')
        self.assertIn('YOUR_GITHUB_USERNAME',guide);self.assertIn('YOUR_ID+',guide)
        self.assertNotIn('git config --global',guide);self.assertIn('自己的GitHub账号',guide)
        self.assertIn('Release标签',guide)
if __name__=='__main__':unittest.main()
