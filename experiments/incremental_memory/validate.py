"""Short targeted synthetic acceptance checks; touches only named lab containers."""

import json
import pathlib
import subprocess
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
results = []


def cli(op, data, success=True):
    r = subprocess.run(
        [
            "docker",
            "exec",
            "andy-memory-lab-core",
            "python",
            "/app/memory.py",
            op,
            json.dumps(data),
        ],
        text=True,
        capture_output=True,
    )
    if success:
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)
    assert r.returncode != 0


def check(name, condition):
    assert condition, name
    results.append(name)


def query(text):
    return cli("query", {"question": text})


def ready():
    for _ in range(40):
        r = subprocess.run(
            [
                "docker",
                "exec",
                "andy-memory-lab-core",
                "python",
                "-c",
                "import memory; memory.connect().close()",
            ],
            capture_output=True,
        )
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise AssertionError("LAB_NOT_READY")


ready()
check(
    "M0 persisted in independent process",
    query("Qual é a cor do carro de Roberto?")["answer"] == "O carro de Roberto é azul.",
)
subprocess.run(
    ["docker", "restart", "--time", "1", "andy-memory-lab-db", "andy-memory-lab-core"],
    check=True,
    capture_output=True,
)
ready()
check(
    "both containers restarted; data survived",
    query("Qual é a cor do carro de Roberto?")["answer"] == "O carro de Roberto é azul.",
)
for text in ("Qual a cor do veículo de Roberto?", "De que cor é o carro de Roberto?"):
    check("equivalent: " + text, query(text)["answer"] == "O carro de Roberto é azul.")
check(
    "second attribute",
    query("Onde Roberto trabalha?")["answer"] == "Roberto trabalha na empresa Alfa.",
)
tag = uuid.uuid4().hex[:8]
for subject, color, company in [
    ("Marta " + tag, "verde", "Beta"),
    ("Caio " + tag, "prata", "Gama"),
]:
    for attr, value, excerpt in [
        ("vehicle.color", color, f"{subject} possui um veículo {color}."),
        ("employment.company", company, f"{subject} trabalha na empresa {company}."),
    ]:
        cli("register", dict(subject=subject, attribute=attr, value=value, excerpt=excerpt))
    check(
        "not fixture-specific " + subject,
        query(f"Qual é a cor do carro de {subject}?")["answer"]
        == f"O carro de {subject} é {color}.",
    )
check(
    "unknown person abstains",
    query("Onde Pessoa inexistente trabalha?")["status"] == "INSUFFICIENT_EVIDENCE",
)
check(
    "unsupported attribute abstains",
    query("Qual é a idade de Roberto?")["status"] == "INSUFFICIENT_EVIDENCE",
)
cli(
    "correct",
    dict(
        subject="Roberto",
        attribute="vehicle.color",
        value="branco",
        excerpt="Correção: Roberto possui um veículo branco.",
        expected=1,
    ),
)
check(
    "correction is current",
    query("Qual é a cor do carro de Roberto?")["answer"] == "O carro de Roberto é branco.",
)
history = cli("history", dict(subject="Roberto", attribute="vehicle.color"))
check(
    "history immutable",
    [(r["revision"], r["value"], r["current"]) for r in history]
    == [(1, "azul", False), (2, "branco", True)],
)
cli(
    "correct",
    dict(
        subject="Roberto",
        attribute="vehicle.color",
        value="preto",
        excerpt="stale correction",
        expected=1,
    ),
    success=False,
)
check(
    "stale correction rejected without new history",
    len(cli("history", dict(subject="Roberto", attribute="vehicle.color"))) == 2,
)
for subject in ("Lia " + tag, "LÍA " + tag):
    cli(
        "register",
        dict(
            subject=subject,
            attribute="vehicle.color",
            value="cinza",
            excerpt=f"{subject} possui um veículo cinza.",
        ),
    )
check(
    "normalized collision ambiguous",
    query("Qual é a cor do carro de Lia " + tag + "?")["status"] == "AMBIGUOUS",
)
subprocess.run(
    ["docker", "stop", "--time", "1", "andy-memory-lab-db"], check=True, capture_output=True
)
try:
    check(
        "database failure is UNAVAILABLE",
        query("Onde Roberto trabalha?")["status"] == "UNAVAILABLE",
    )
finally:
    subprocess.run(["docker", "start", "andy-memory-lab-db"], check=True, capture_output=True)
ready()
# Check OS network namespace and effective SQL privileges, without probing any service.
for container in ("andy-memory-lab-core", "andy-memory-lab-db"):
    r = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
    check(container + " network none", r["HostConfig"]["NetworkMode"] == "none")
    check(container + " no ports", not r["HostConfig"]["PortBindings"])
    check(container + " no privilege", not r["HostConfig"]["Privileged"])
    check(container + " no restart", r["HostConfig"]["RestartPolicy"]["Name"] == "no")
    check(
        container + " exclusive named volumes",
        all(
            m.get("Name", "").startswith("andy-memory-lab-")
            for m in r["Mounts"]
            if m["Type"] == "volume"
        ),
    )
    check(
        container + " exclusive bind mounts",
        all(m["Source"].startswith(str(ROOT) + "/") for m in r["Mounts"] if m["Type"] == "bind"),
    )
    devices = subprocess.check_output(
        ["docker", "exec", container, "cat", "/proc/net/dev"], text=True
    )
    check(
        container + " loopback only",
        [line.split(":")[0].strip() for line in devices.splitlines() if ":" in line] == ["lo"],
    )
    routes = subprocess.check_output(
        ["docker", "exec", container, "cat", "/proc/net/route"], text=True
    )
    check(container + " no routes", len(routes.strip().splitlines()) == 1)
code = """import memory
for write in (False,True):
 with memory.connect(write) as db:
  r=db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
  assert not any(r.values())
  assert not db.execute("SELECT has_table_privilege(current_user,'evidence','UPDATE') AS p").fetchone()['p']
  assert not db.execute("SELECT has_table_privilege(current_user,'revision','DELETE') AS p").fetchone()['p']
  assert not db.execute("SHOW listen_addresses").fetchone()['listen_addresses']
with memory.connect() as db:
 assert not db.execute("SELECT has_table_privilege(current_user,'evidence','INSERT') AS p").fetchone()['p']
print('privileges verified')
"""
subprocess.run(
    ["docker", "exec", "andy-memory-lab-core", "python", "-c", code],
    check=True,
    capture_output=True,
)
check("minimum SQL privileges and no TCP listener", True)
(ROOT / "evidence/validation.json").write_text(
    json.dumps({"passed": results, "history": history}, ensure_ascii=False, indent=2)
)
print(json.dumps({"passed": len(results), "checks": results}, ensure_ascii=False, indent=2))
