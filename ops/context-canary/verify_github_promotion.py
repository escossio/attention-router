"""Read-only preflight for the GitHub-first promotion contract in issue #428."""

import argparse
import json
import re
import runpy
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
SINGLE_OWNER_MODE = "single_owner"
SINGLE_OWNER_ASSURANCE = "automated_evidence_not_independent_human_review"
SINGLE_OWNER_APPS = {
    name: 4982054 if name.startswith("distributed-") else 15368
    for name in REQUIRED_CHECKS
}
V2_OPERATION_FIELDS = {
    "version", "record_type", "mode", "gate", "operation", "pr", "sha",
    "approval_ref", "expires_at", "artifact_sha256",
    "technical_acceptance_id", "technical_acceptance_sha256",
}
V2_TECHNICAL_FIELDS = {
    "version", "record_type", "mode", "decision", "assurance", "gate",
    "operation", "pr", "sha", "head", "tree", "artifact_sha256",
    "expires_at", "checks",
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
    # Only the two fixed metadata endpoints may leave the repository namespace.
    if endpoint == "@authenticated-user":
        path = "user"
    elif endpoint == "@repository":
        path = f"repos/{REPO}"
    else:
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


def record_body(comment):
    require(comment["user"]["login"] == OWNER, "aprovação ausente ou autor divergente")
    require(comment["issue_url"] == f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
            "aprovação fora da issue #428")
    body = re.fullmatch(r"```json\s*(\{.*\})\s*```", comment["body"].strip(), re.S)
    require(body is not None, "registro de promoção inválido")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "campo duplicado no registro de promoção")
            result[key] = value
        return result

    return json.loads(body.group(1), object_pairs_hook=unique_pairs)


def utc_time(value):
    require(isinstance(value, str), "timestamp V2 inválido")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PromotionBlocked("timestamp V2 inválido") from exc
    require(parsed.utcoffset() == timedelta(0), "timestamp V2 deve usar UTC")
    return parsed


def immutable_comment_window(comment, record, now, maximum):
    created = utc_time(comment.get("created_at"))
    updated = utc_time(comment.get("updated_at"))
    expires = utc_time(record.get("expires_at"))
    require(created == updated, "registro V2 editado ou revogado")
    require(created <= now < expires and timedelta(0) < expires - created <= maximum,
            "registro V2 fora da janela")
    return created, expires


def approval_record(comment, operation, now):
    record = record_body(comment)
    require(type(record.get("version")) is int and record["version"] in {1, 2}
            and record["gate"] == OPERATIONS[operation]
            and record["operation"] == operation, "gate ou operação divergente")
    if record["version"] == 2:
        require(set(record) == V2_OPERATION_FIELDS
                and record["record_type"] == "operation_authorization"
                and record["mode"] == SINGLE_OWNER_MODE, "contrato operacional V2 inválido")
        require(type(record["technical_acceptance_id"]) is int and record["technical_acceptance_id"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", record["technical_acceptance_sha256"]),
                "referência ao aceite técnico V2 inválida")
        immutable_comment_window(comment, record, now, timedelta(minutes=30))
    if record["version"] == 1:
        require(not (set(record) & {"mode", "record_type", "technical_acceptance_id",
                                    "technical_acceptance_sha256"}),
                "registro V1 não pode declarar modalidade V2")
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


def single_owner_identity(fetch, pr, *comments):
    """Account/collaborator separation; not an inventory of every token or App."""
    repository = fetch("@repository")
    actor = fetch("@authenticated-user")
    owner = repository.get("owner") or {}
    uid = owner.get("id")
    require(repository.get("full_name") == REPO and repository.get("default_branch") == "main"
            and owner.get("login") == OWNER and owner.get("type") == "User"
            and type(uid) is int and uid > 0
            and (repository.get("permissions") or {}).get("admin") is True
            and actor.get("login") == OWNER and actor.get("id") == uid
            and actor.get("type") == "User",
            "identidade ou autoridade do proprietário único não comprovada")
    require(pr.get("user", {}).get("login") == OWNER and pr["user"].get("id") == uid
            and pr.get("author_association") == "OWNER"
            and (pr.get("head", {}).get("repo") or {}).get("full_name") == REPO,
            "PR não pertence ao proprietário único ou ao repositório")
    for comment in comments:
        require(comment.get("user", {}).get("login") == OWNER and comment["user"].get("id") == uid,
                "identidade do autor do registro V2 divergente")

    collaborators = fetch("collaborators?affiliation=all&per_page=100", key="")
    require(isinstance(collaborators, list) and collaborators,
            "inventário de colaboradores indisponível")
    seen_ids, seen_logins = set(), set()
    authorities = []
    for collaborator in collaborators:
        cid, login = collaborator.get("id"), collaborator.get("login")
        permissions = collaborator.get("permissions")
        role = collaborator.get("role_name")
        require(type(cid) is int and cid > 0 and isinstance(login, str) and login
                and cid not in seen_ids and login.casefold() not in seen_logins
                and collaborator.get("type") in {"User", "Bot"}
                and role in {"read", "triage", "write", "maintain", "admin"}
                and isinstance(permissions, dict)
                and all(type(permissions.get(key)) is bool for key in
                        ("pull", "triage", "push", "maintain", "admin")),
                "inventário de colaboradores incompleto ou divergente")
        seen_ids.add(cid)
        seen_logins.add(login.casefold())
        privileged = role in {"write", "maintain", "admin"} or any(
            permissions[key] for key in ("push", "maintain", "admin")
        )
        if privileged:
            authorities.append((cid, login))
        if cid == uid:
            require(login == OWNER and collaborator["type"] == "User"
                    and role == "admin" and permissions["admin"] and permissions["push"],
                    "inventário não confirma autoridade do proprietário")
    require(authorities == [(uid, OWNER)], "outra autoridade de escrita ou administração presente")
    return uid


def single_owner_acceptance(fetch, pr, reviews, comment_id, comment, record, now):
    require(type(comment_id) is int and comment_id > 0 and comment.get("id") == comment_id,
            "identidade do registro operacional V2 divergente")
    acceptance_id = record["technical_acceptance_id"]
    require(acceptance_id != comment_id, "aceite técnico deve ser um registro distinto")
    technical = fetch(f"issues/comments/{acceptance_id}")
    require(technical.get("id") == acceptance_id, "identidade do aceite técnico divergente")
    accepted = record_body(technical)
    require(set(accepted) == V2_TECHNICAL_FIELDS
            and type(accepted.get("version")) is int and accepted["version"] == 2
            and accepted["record_type"] == "technical_acceptance"
            and accepted["mode"] == SINGLE_OWNER_MODE and accepted["decision"] == "accepted"
            and accepted["assurance"] == SINGLE_OWNER_ASSURANCE,
            "aceite técnico V2 ausente, revogado ou inválido")
    require(sha256(technical["body"].encode()).hexdigest() == record["technical_acceptance_sha256"],
            "digest do aceite técnico divergente")
    require(all(accepted[k] == record[k] for k in ("gate", "operation", "pr", "sha", "artifact_sha256"))
            and type(accepted["pr"]) is int
            and accepted["head"] == pr["head"]["sha"]
            and re.fullmatch(r"[0-9a-f]{40}", accepted["head"])
            and re.fullmatch(r"[0-9a-f]{40}", accepted["tree"]),
            "aceite técnico pertence a outra operação, PR, SHA ou artefato")
    accepted_at, accepted_until = immutable_comment_window(technical, accepted, now, timedelta(hours=24))
    authorized_at, authorized_until = immutable_comment_window(comment, record, now, timedelta(minutes=30))
    require(accepted_at < authorized_at and authorized_until <= accepted_until,
            "aceite técnico deve preceder e abranger a autorização operacional")
    require(utc_time(pr.get("merged_at")) <= accepted_at, "aceite anterior à integração do PR")
    # Never ignore a pending objection just because the owner selected V2.
    decisions = {}
    for review in sorted(reviews, key=lambda item: (item["submitted_at"] or "", item["id"])):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            decisions[review["user"]["login"]] = review
    require(not any(r["state"] == "CHANGES_REQUESTED" for r in decisions.values()),
            "PR ainda tem pedido de alteração")
    single_owner_identity(fetch, pr, comment, technical)
    return accepted, technical, accepted_at


def single_owner_policy(protection, rules, expected, classic):
    require(classic.get("required_status_checks") == protection,
            "proteções clássicas divergentes entre consultas")
    require(classic.get("enforce_admins", {}).get("enabled") is True,
            "proteção clássica não se aplica ao administrador")
    classic_reviews = classic.get("required_pull_request_reviews")
    require(classic_reviews is None or (
                classic_reviews.get("required_approving_review_count") == 0
                and classic_reviews.get("require_code_owner_reviews") is not True
                and classic_reviews.get("require_last_push_approval") is not True),
            "revisão clássica obrigatória incompatível com proprietário único")
    require(protection.get("strict") is True
            and all(expected.get(name) == app for name, app in SINGLE_OWNER_APPS.items()),
            "política estrita ou Apps oficiais V2 divergentes")
    strict = [r["parameters"] for r in rules if r["type"] == "required_status_checks"
              and r["parameters"].get("strict_required_status_checks_policy") is True]
    strict_apps = {(c["context"], c.get("integration_id")) for p in strict
                   for c in p["required_status_checks"]}
    require(set(SINGLE_OWNER_APPS.items()) <= strict_apps,
            "ruleset estrito V2 incompleto")
    pull_rules = [r["parameters"] for r in rules if r["type"] == "pull_request"]
    require(pull_rules and all(type(p.get("required_approving_review_count")) is int
                and p["required_approving_review_count"] == 0
                and p.get("require_code_owner_review") is False
                and p.get("require_last_push_approval") is False
                and p.get("required_review_thread_resolution") is True
                and p.get("required_reviewers") == []
                for p in pull_rules),
            "regras de revisão do repositório incompatíveis com proprietário único")


def check_identities(sha, checks, expected, accepted_at):
    """Pin the latest official result and its receipt on each exact SHA."""
    verify_checks(sha, checks, expected)
    result = {}
    seen = set()
    for name, app in sorted(expected.items()):
        matching = [r for r in checks if r["name"] == name and r["app"]["id"] == app
                    and r["head_sha"] == sha]
        require(all(type(r.get("id")) is int and r["id"] > 0 for r in matching)
                and len({r["id"] for r in matching}) == len(matching),
                "identidade de check V2 inválida")
        latest = max(matching, key=lambda r: r["id"])
        require(latest["id"] not in seen and utc_time(latest.get("completed_at")) <= accepted_at,
                "check V2 duplicado ou concluído após o aceite técnico")
        seen.add(latest["id"])
        output = latest.get("output")
        require(isinstance(output, dict), "conteúdo do check V2 ausente")
        if name == "distributed-postgres":
            summary = output.get("summary", "")
            passed = re.findall(r"^Tests passed: ([1-9][0-9]*)$", summary, re.M)
            selected = re.findall(r"^Selected tests: ([1-9][0-9]*)$", summary, re.M)
            require(len(passed) == len(selected) == 1 and passed == selected,
                    "execução PostgreSQL V2 não cobre toda a seleção")
        result[name] = {
            "app_id": app, "check_run_id": latest["id"], "completed_at": latest["completed_at"],
            "output_sha256": sha256(json.dumps(output, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode()).hexdigest(),
        }
    return result


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
    reviews = fetch(f"pulls/{number}/reviews?per_page=100", key="")
    single = record["version"] == 2
    if single:
        accepted, technical, accepted_at = single_owner_acceptance(
            fetch, pr, reviews, comment_id, comment, record, now or datetime.now(UTC))
    else:
        verify_reviews(pr, reviews)

    ancestry = fetch(f"compare/{sha}...main?per_page=1")
    require(ancestry["merge_base_commit"]["sha"] == sha and ancestry["status"] in {"ahead", "identical"},
            "SHA aprovado diverge da linhagem atual da main")
    commit = fetch(f"git/commits/{sha}")
    require(commit["sha"] == sha, "GitHub retornou commit divergente")
    protection = fetch("branches/main/protection/required_status_checks")
    branch_rules = fetch("rules/branches/main?per_page=100", key="")
    expected = required_check_apps(protection["checks"], branch_rules)
    if single:
        require(commit["tree"]["sha"] == accepted["tree"],
                "árvore integrada diverge do aceite técnico")
        parents = commit.get("parents") or []
        require(len(parents) == 2 and parents[1]["sha"] == accepted["head"],
                "merge V2 não preserva o head final")
        single_owner_policy(protection, branch_rules, expected, fetch("branches/main/protection"))
    checks = fetch(f"commits/{sha}/check-runs?filter=all&per_page=100", key="check_runs")
    if single:
        head_sha = accepted["head"]
        head_checks = fetch(f"commits/{head_sha}/check-runs?filter=all&per_page=100", key="check_runs")
        snapshots = {"head": check_identities(head_sha, head_checks, expected, accepted_at),
                     "integrated": check_identities(sha, checks, expected, accepted_at)}
        identities = [r["check_run_id"] for scope in snapshots.values() for r in scope.values()]
        require(len(identities) == len(set(identities)), "check V2 reutilizado entre SHAs")
        require(accepted["checks"] == snapshots, "identidade ou recibo de check diverge do aceite técnico")
    else:
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
    if single:
        # Recheck the two records and sole authority immediately before returning.
        # No approval is created here; a revoked/edited/deleted record fails closed.
        fresh = fetch(f"issues/comments/{comment_id}")
        fresh_technical = fetch(f"issues/comments/{record['technical_acceptance_id']}")
        require(fresh == comment and fresh_technical == technical,
                "registro V2 alterado durante o pré-voo")
        fresh_pr = fetch(f"pulls/{number}")
        require(fresh_pr["merged"] and fresh_pr["merge_commit_sha"] == sha
                and fresh_pr["head"]["sha"] == accepted["head"]
                and fresh_pr["base"]["ref"] == "main"
                and fresh_pr["base"]["repo"]["full_name"] == REPO
                and re.search(rf"(?:^|[\s(\[<*`]){gate_ref}(?![\w/-])", fresh_pr["body"] or ""),
                "PR mudou durante o pré-voo V2")
        fresh_reviews = fetch(f"pulls/{number}/reviews?per_page=100", key="")
        require(fresh_reviews == reviews, "revisões mudaram durante o pré-voo V2")
        single_owner_identity(fetch, fresh_pr, fresh, fresh_technical)
        fresh_protection = fetch("branches/main/protection/required_status_checks")
        fresh_rules = fetch("rules/branches/main?per_page=100", key="")
        fresh_expected = required_check_apps(fresh_protection["checks"], fresh_rules)
        single_owner_policy(fresh_protection, fresh_rules, fresh_expected,
                            fetch("branches/main/protection"))
        require(fresh_expected == expected, "checks obrigatórios mudaram durante o pré-voo V2")
        fresh_ancestry = fetch(f"compare/{sha}...main?per_page=1")
        require(fresh_ancestry["merge_base_commit"]["sha"] == sha
                and fresh_ancestry["status"] in {"ahead", "identical"},
                "linhagem mudou durante o pré-voo V2")
        for scope, target in (("head", accepted["head"]), ("integrated", sha)):
            fresh_checks = fetch(f"commits/{target}/check-runs?filter=all&per_page=100",
                                 key="check_runs")
            require(check_identities(target, fresh_checks, expected, accepted_at) == snapshots[scope],
                    "checks mudaram durante o pré-voo V2")
        verify_checkout(root, sha, commit["tree"]["sha"])
        current_time = now or datetime.now(UTC)
        immutable_comment_window(fresh, record, current_time, timedelta(minutes=30))
        immutable_comment_window(fresh_technical, accepted, current_time, timedelta(hours=24))
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
