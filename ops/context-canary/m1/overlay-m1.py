from pathlib import Path

path=Path('tests/integration/test_postgres_owner_scope_registry.py')
source=path.read_text()
changes={
    '    key = b"synthetic-owner-e5-anchor-key-for-ci-only-32"': '''    if prod_canary:
        import m1_owner
        m1_owner.trace_failures(monkeypatch)
        m1_world = m1_owner.seed(Session, lab, owner_writer, args["bearer"], scope_ref, decision)

    key = b"synthetic-owner-e5-anchor-key-for-ci-only-32"''',
    '    for _ in range(40):': '    for _ in range(280 if prod_canary else 40):',
    '''            _probe_runtime_contention(
                client, path, headers, scope_ref, decision("confirm")["message_id"],
                tmp_path / "scope-anchor.sqlite", runtime_engine, caplog,
            )''': '''            m1_owner.run(client, path, headers, scope_ref, tmp_path / "scope-anchor.sqlite",
                         caplog, Session, monkeypatch, m1_world)''',
}
for before,after in changes.items():
    assert source.count(before)==1, before
    source=source.replace(before,after)
path.write_text(source)

source = path.read_text()
markers = {
    '            assert reopened == earlier\n': (
        '            assert reopened == earlier\n'
        '            print("M1_REOPEN=PASS", flush=True)\n'
    ),
    '        assert refused.status_code == 503 and refused.content == b""\n': (
        '        assert refused.status_code == 503 and refused.content == b""\n'
        '        print("M1_REVOCATION=PASS", flush=True)\n'
    ),
}
for before, after in markers.items():
    assert source.count(before) == 1, before
    source = source.replace(before, after)
path.write_text(source)

source = path.read_text()
before = 'os.pathsep.join((str(Path(__file__).parent), str(Path.cwd())))'
after = (
    'os.pathsep.join((str(Path(__file__).parent), str(Path.cwd()), '
    'os.environ["M1_VECTOR_LIBS"]))'
)
assert source.count(before) == 1
path.write_text(source.replace(before, after))
