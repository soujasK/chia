import sqlite3

conn = sqlite3.connect('chia/examples/circt_issue_solver/issues.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=== ATTEMPTS SCHEMA ===")
for col in c.execute("PRAGMA table_info(attempts)").fetchall():
    print(dict(col))

print("\n=== ATTEMPTS DATA ===")
for r in c.execute('SELECT * FROM attempts').fetchall():
    d = dict(r)
    print(f"ID {d.get('id')} | Issue #{d.get('issue_number')} | Status: {d.get('status')} | Fixed: {d.get('fixed')} | LitOK: {d.get('lit_ok')} | Model: {d.get('llm_model')}")
    if d.get('notes'):
        print(f"   Notes: {d.get('notes')[:150]}")

print("\n=== GATE METRICS DATA ===")
for r in c.execute('SELECT * FROM gate_metrics').fetchall():
    d = dict(r)
    print(f"ID {d.get('id')} | Issue #{d.get('issue_number')} | Enabled: {d.get('gate_enabled')} | Status: {d.get('status')} | RawOps: {d.get('raw_ir_op_count')} | FinalOps: {d.get('final_op_count')}")
