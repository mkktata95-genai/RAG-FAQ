python -c "
import json
data = json.loads(open('table_audit_step1.json').read())
print('Total:', len(data))
classes = set(r.get('classification') for r in data)
print('Classifications found:', classes)
table = [r for r in data if r.get('classification') != 'NO_TABLE']
print('Table pages:', len(table))
print('Sample keys:', list(data[0].keys()) if data else 'empty')
"