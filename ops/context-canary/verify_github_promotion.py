"""Read-only preflight for the GitHub-first promotion contract in issue #428."""

import argparse
import json
import re
import runpy
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

REPO = "escossio/attention-router"
OWNER = "escossio"
ISSUE = 428
IMAGE = f"ghcr.io/{REPO}"
PUBLISH_WORKFLOW = f"{REPO}/.github/workflows/private-context-oci.yml"
OPERATIONS = {
    "schema_apply": "G3",
    "schema_rollback": "G3",
    "roles_apply": "G4",
    "roles_rollback": "G4",
    "runtime_apply": "G5",
    "runtime_rollback": "G5",
    "canary_enable": "G6",
    "canary_disable": "G6",
}
REQUIRED_CHECKS = {
    "python-tests", "transport-tests", "docker-build", "secret-scan",
    "analyze (python)", "analyze (javascript-typescript)", "distributed-postgres",
    "distributed-python",
}
DISTRIBUTED_TITLES = {
    "distributed-python": "Distributed python shadow gate passed",
    "distributed-postgres": "Distributed PostgreSQL gate passed",
}


class PromotionBlocked(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise PromotionBlocked(reason)


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=45)
    return result.stdout


def github(endpoint, *, key=None):
    path = f"repos/{REPO}/{endpoint}"
    if key is None:
        return json.loads(run("gh", "api", "--hostname", "github.com", path))
    # gh emits each page as a separate JSON value. Select one value per line.
    query = ".[]" if key == "" else f".{key}[]"
    return [
        json.loads(line)
        for line in run("gh", "api", "--hostname", "github.com", "--paginate", path, "--jq", query)
        .splitlines()
    ]


def approval_record(comment, operation, now):
    require(comment["user"]["login"] == OWNER, "aprovação ausente ou autor divergente")
    require(comment["issue_url"] == f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
            "aprovação fora da issue #428")
    body = re.fullmatch(r"```json\s*(\{.*\})\s*```", comment["body"].strip(), re.S)
    require(body is not None, "registro de promoção inválido")
    record = json.loads(body.group(1))
    require(record["version"] == 1 and record["gate"] == OPERATIONS[operation]
            and record["operation"] == operation, "gate ou operação divergente")
    require(type(record["pr"]) is int and record["pr"] > 0, "PR inválido")
    require(re.fullmatch(r"[0-9a-f]{40}", record["sha"]), "SHA de promoção inválido")
    require(re.fullmatch(r"[0-9A-Za-z_-]{1,80}", record["approval_ref"]),
            "referência da autorização ausente ou inválida")
    issued = datetime.fromisoformat(comment["updated_at"])
    expires = datetime.fromisoformat(record["expires_at"])
    require(issued <= now < expires, "autorização fora da janela")
    return record


def verify_checkout(root, sha, tree):
    def git(*args):
        return run("git", "-C", str(root), *args)

    require(Path(git("rev-parse", "--show-toplevel").strip()).resolve() == root.resolve(),
            "diretório de release precisa ser a raiz do checkout")
    require(git("rev-parse", "HEAD").strip() == sha, "SHA local diverge do SHA certificado")
    require(git("rev-parse", "HEAD^{tree}").strip() == tree, "árvore local diverge do GitHub")
    require(all(line.startswith("H ") for line in git("ls-files", "-v").splitlines()),
            "índice contém entradas ocultas ou não verificáveis")
    require(not git("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
                    "--ignore-submodules=none").strip(),
            "checkout com alterações, arquivos ignorados ou sobreposições locais")


def verify_reviews(pr, reviews):
    decisions = {}
    for review in sorted(reviews, key=lambda item: (item["submitted_at"] or "", item["id"])):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            decisions[review["user"]["login"]] = review
    final_head = pr["head"]["sha"]
    require(any(
        login != pr["user"]["login"] and review["state"] == "APPROVED"
        and review["commit_id"] == final_head
        and review["author_association"] in {"OWNER", "MEMBER", "COLLABORATOR"}
        for login, review in decisions.items()
    ), "revisão favorável ausente no head final do PR")
    require(not any(review["state"] == "CHANGES_REQUESTED" for review in decisions.values()),
            "PR ainda tem pedido de alteração")


def required_check_apps(protection, branch_rules):
    entries = [(check["context"], check.get("app_id")) for check in protection]
    for rule in branch_rules:
        if rule["type"] == "required_status_checks":
            entries.extend(
                (check["context"], check.get("integration_id"))
                for check in rule["parameters"]["required_status_checks"]
            )
    expected = {}
    for name, app_id in entries:
        require(type(app_id) is int and app_id > 0, f"App obrigatório sem vínculo: {name}")
        require(expected.setdefault(name, app_id) == app_id, f"Apps obrigatórios divergentes: {name}")
    require(REQUIRED_CHECKS <= expected.keys(), "política de checks obrigatórios incompleta")
    return expected


def verify_distributed_execution(name, sha, output):
    reason = f"recibo de execução distribuída ausente ou divergente: {name}"
    require(isinstance(output, dict) and output.get("title") == DISTRIBUTED_TITLES[name], reason)
    summary = output.get("summary")
    require(isinstance(summary, str), reason)

    def field(label, pattern):
        values = [line.removeprefix(label) for line in summary.splitlines() if line.startswith(label)]
        require(len(values) == 1 and re.fullmatch(pattern, values[0]), reason)
        return values[0]

    require(field("Exact SHA: ", r"[0-9a-f]{40}") == sha, reason)
    if name == "distributed-python":
        field("Suite: ", "python")
        worker = field("Selected worker: ", r"[a-z][a-z0-9_-]*")
        attempts = re.findall(r"^\| [1-9][0-9]* \| ([a-z][a-z0-9_-]*) \| PASS \| [0-9]+ s \|$", summary, re.M)
        require(worker != "none" and attempts == [worker], reason)
        return

    passed = int(field("Tests passed: ", r"[1-9][0-9]*"))
    tests = int(field("Selected tests: ", r"[1-9][0-9]*"))
    files = int(field("Selected files: ", r"[1-9][0-9]*"))
    field("Selection SHA-256: ", r"[0-9a-f]{64}")
    rows = [line for line in summary.splitlines() if line.startswith("| CI03 |")]
    require(len(rows) == 1 and re.fullmatch(rf"\| CI03 \| PASS \| [0-9]+ s \| {passed} \| {files} \|", rows[0])
            and max(passed, files) <= tests, reason)


def verify_checks(sha, checks, expected):
    for name, app_id in sorted(expected.items()):
        matching = [check for check in checks
                    if check["name"] == name and check["app"]["id"] == app_id
                    and check["head_sha"] == sha]
        latest = max(matching, key=lambda check: check["id"], default={})
        require(latest.get("status") == "completed" and latest.get("conclusion") == "success",
                f"check ausente ou sem sucesso no SHA: {name}")
        if name in DISTRIBUTED_TITLES:
            verify_distributed_execution(name, sha, latest.get("output"))


def verify_private_oci(root, archive, sha, digest):
    require(archive is not None, "arquivo OCI necessário para a atestação")
    path = Path(archive)
    require(path.is_file() and not path.is_symlink(), "arquivo OCI ausente ou inválido")
    try:
        run("gh", "attestation", "verify", str(path.resolve()), "--repo", REPO,
            "--signer-workflow", PUBLISH_WORKFLOW, "--source-ref", "refs/heads/main",
            "--source-digest", sha, "--deny-self-hosted-runners")
    except (OSError, subprocess.SubprocessError) as exc:
        raise PromotionBlocked("atestado OCI ausente, divergente ou CLI incompatível") from exc
    inspect = runpy.run_path(str(root / "ops/context-canary/build_private_oci.py"))["inspect_image"]
    try:
        manifest, image_id = inspect(path, sha, with_config=True)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise PromotionBlocked("conteúdo do arquivo OCI inválido") from exc
    require(manifest == f"sha256:{digest}", "manifesto OCI diverge da aprovação")
    return image_id


def verify(comment_id, operation, root, artifact, *, archive=None, approval_ref=None, fetch=github, now=None,
           return_record=False):
    comment = fetch(f"issues/comments/{comment_id}")
    record = approval_record(comment, operation, now or datetime.now(UTC))
    if approval_ref is not None:
        require(record["approval_ref"] == approval_ref, "autorização privada diverge da aprovação GitHub")
    sha, number = record["sha"], record["pr"]
    pr = fetch(f"pulls/{number}")
    require(pr["merged"] and pr["merge_commit_sha"] == sha
            and pr["base"]["repo"]["full_name"] == REPO and pr["base"]["ref"] == "main",
            "SHA não corresponde à integração do PR na main")
    gate_ref = rf"(?:#|{re.escape(REPO)}#|https://github\.com/{re.escape(REPO)}/issues/){ISSUE}"
    require(re.search(rf"(?:^|[\s(\[<*`]){gate_ref}(?![\w/-])", pr["body"] or ""),
            "PR sem referência ao gate #428")
    verify_reviews(pr, fetch(f"pulls/{number}/reviews?per_page=100", key=""))

    ancestry = fetch(f"compare/{sha}...main?per_page=1")
    require(ancestry["merge_base_commit"]["sha"] == sha and ancestry["status"] in {"ahead", "identical"},
            "SHA aprovado diverge da linhagem atual da main")
    commit = fetch(f"git/commits/{sha}")
    require(commit["sha"] == sha, "GitHub retornou commit divergente")
    protection = fetch("branches/main/protection/required_status_checks")["checks"]
    branch_rules = fetch("rules/branches/main?per_page=100", key="")
    expected = required_check_apps(protection, branch_rules)
    checks = fetch(f"commits/{sha}/check-runs?filter=all&per_page=100", key="check_runs")
    verify_checks(sha, checks, expected)
    verify_checkout(root, sha, commit["tree"]["sha"])

    expected_digest = record.get("artifact_sha256", "")
    image_id = None
    if record["gate"] in {"G3", "G4"}:
        require(re.fullmatch(r"[0-9a-f]{64}", expected_digest) and artifact == expected_digest,
                "digest do runner diverge da aprovação")
    else:
        require(re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                and artifact == f"{IMAGE}@sha256:{expected_digest}",
                "imagem OCI ausente ou digest divergente da aprovação")
        image_id = verify_private_oci(root, archive, sha, expected_digest)
    if now is None:
        require(datetime.now(UTC) < datetime.fromisoformat(record["expires_at"]),
                "autorização expirou durante o pré-voo")
    if return_record:
        return record if image_id is None else {**record, "oci_image_id": image_id}
    return sha, number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("comment_id", type=int)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--artifact", help="digest do runner ou referência ao manifesto OCI aprovado")
    parser.add_argument("--archive", type=Path, help="arquivo OCI atestado pelo workflow privado")
    args = parser.parse_args()
    try:
        record = verify(args.comment_id, args.operation, args.root, args.artifact,
                        archive=args.archive, return_record=True)
    except PromotionBlocked as exc:
        parser.exit(1, f"PROMOÇÃO BLOQUEADA: {exc}\n")
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        parser.exit(1, "PROMOÇÃO BLOQUEADA: falha de leitura ou formato inválido\n")
    print(f"PREFLIGHT_APROVADO {args.operation} #{ISSUE} PR #{record['pr']} {record['sha']}")
    if image_id := record.get("oci_image_id"):
        print(f"OCI_IMAGE_ID={image_id}")


if __name__ == "__main__":
    main()
