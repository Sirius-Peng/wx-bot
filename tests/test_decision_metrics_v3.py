"""Guard the selective classifier's quality gate; speed never substitutes for quality."""
import math,unittest
from tools.decision_model.metrics import score,quality_gate
from src.core.local_decision_worker import calibrated_probabilities,encode_sequence
ACTIONS=('reply','defer','context_only')

class MetricsTests(unittest.TestCase):
    def test_all_abstention_cannot_pass_coverage_gate(self):
        rows=[{'label':a,'protected':a=='reply'} for a in ACTIONS for _ in range(120)]
        ps=[dict(zip(ACTIONS,[1/3]*3)) for _ in rows]
        m=score(rows,ps)
        self.assertEqual(m['reply_recall'],1)
        self.assertEqual(m['coverage'],0)
        self.assertIsNone(m['silent_precision'])
        self.assertFalse(quality_gate(m,latency_p95_ms=1,flips=0)['passed'])
    def test_typed_correct_decisions_pass_contract_not_arbitrary_softmax(self):
        rows=[{'label':a,'protected':a=='reply'} for a in ACTIONS for _ in range(120)]
        ps=[{a:.998 if a==r['label'] else .001 for a in ACTIONS} for r in rows]
        self.assertTrue(quality_gate(score(rows,ps),latency_p95_ms=299,flips=0)['passed'])
    def test_false_silence_is_not_counted_as_recall(self):
        rows=[{'label':'reply','protected':True}]*200
        ps=[{'reply':.001,'defer':.998,'context_only':.001}]+[{'reply':.998,'defer':.001,'context_only':.001}]*199
        m=score(rows,ps)
        self.assertEqual(m['protected_missed'],1);self.assertEqual(m['reply_recall'],.995)
        self.assertFalse(quality_gate(m,latency_p95_ms=1,flips=0)['passed'])
    def test_misaligned_or_nan_outputs_do_not_get_metrics(self):
        with self.assertRaises(ValueError):score([{'label':'reply'}],[])
        with self.assertRaises(ValueError):score([{'label':'reply'}],[{'reply':math.nan,'defer':0,'context_only':0}])
    def test_softmax_has_exactly_three_finite_logits(self):
        for logits,cal in [([1,2,3,4],{'temperature':1}),([1,2,3],{'temperature':math.nan}),([1,2,3],{'temperature':1,'bias':[1,2]})]:
            with self.assertRaises(ValueError):calibrated_probabilities(logits,cal)
        p=calibrated_probabilities([10000,9999,9998],{'temperature':2})
        self.assertAlmostEqual(sum(p.values()),1);self.assertEqual(max(p,key=p.get),'reply')
    def test_overlong_complete_input_abstains_instead_of_truncating(self):
        class Tok:
            def encode(self,text,**kw):return type('Encoded',(),{'ids':list(range(len(text)))})()
        policy={'architecture':'fixed-choice-v1','max_length':30,'special_tokens':{'cls':2,'sep':1,'mask':4}}
        with self.assertRaisesRegex(ValueError,'input_too_large'):encode_sequence(Tok(),{'current':'完整来话'*100},policy)

if __name__=='__main__':unittest.main()
