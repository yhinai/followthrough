import json
from pathlib import Path
from followthrough.classifier import classify

cases = json.loads(Path(__file__).parents[1].joinpath('evals/cases.json').read_text())
results = []
for case in cases:
    result = classify(case['input'])
    actual = 'actionable' if result.actionable else result.kind
    results.append({**case, 'actual': actual, 'ok': actual == case['expected']})
print(json.dumps({'passed': sum(x['ok'] for x in results), 'total': len(results), 'cases': results}, indent=2))
raise SystemExit(0 if all(x['ok'] for x in results) else 1)
