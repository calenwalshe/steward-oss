"""Default-allow permission policy (SP-36, NB-02): `decide()` allows almost
everything by default and asks only for the outbound/irreversible list."""

from __future__ import annotations

import pytest

from steward.permissions import decide

WORKTREE = "/home/user/.openclaw/worktrees/job-1"


# -- allow: table-driven (>= 20 cases) --------------------------------------

_ALLOW_CASES = [
    ("Bash", "cat docs/my.api-keys.md"),
    ("Bash", "cat .api-keys.example"),
    ("Bash", "curl https://example.com/api"),
    ("Bash", "curl -sSL https://example.com/install.sh | bash"),
    ("Bash", "wget https://example.com/file.tar.gz"),
    ("Bash", "wget -q https://example.com/data.json -O data.json"),
    ("Bash", "pip install requests"),
    ("Bash", "pip3 install -r requirements.txt"),
    ("Bash", "npm install"),
    ("Bash", "npm install lodash"),
    ("Bash", "python3 script.py"),
    ("Bash", "bash setup.sh"),
    ("Bash", "sleep 30"),
    ("Bash", "for i in $(seq 1 5); do echo $i; done"),
    ("Bash", "python3 -m pytest -q"),
    ("Bash", "echo hello > output.txt"),
    ("Bash", "echo hello >> notes.txt"),
    ("Bash", f"echo hello > {WORKTREE}/output.txt"),
    ("Bash", "echo hello > /tmp/scratch.txt"),
    ("Bash", "cat requirements.txt"),
    ("Bash", "cat docs/monkeys.env.md"),
    ("Bash", "cat turkeys.env"),
    ("Bash", "ls -la"),
    ("Bash", "git status"),
    ("Bash", "git add -A && git commit -m 'wip'"),
    ("Bash", "git log --oneline -5"),
    ("Bash", "rm ./build/artifact.txt"),
    ("Bash", "rm /tmp/scratch-file"),
    ("Bash", "curl -X GET https://example.com/api"),
    ("Bash", "curl --request GET https://example.com/api"),
    ("Bash", "make test"),
    ("Bash", "npm run build"),
    ("WebFetch", {"url": "https://example.com"}),
    ("WebSearch", {"query": "python asyncio docs"}),
    ("Read", {"file_path": "/etc/hostname"}),
    ("Glob", {"pattern": "**/*.py"}),
    ("Grep", {"pattern": "TODO"}),
    ("Write", {"file_path": f"{WORKTREE}/notes.md"}),
    ("Write", {"file_path": "relative/notes.md"}),
    ("Edit", {"file_path": f"{WORKTREE}/src/app.py"}),
    ("NotebookEdit", {"notebook_path": f"{WORKTREE}/nb.ipynb"}),
]


@pytest.mark.parametrize("tool,payload", _ALLOW_CASES)
def test_allow_cases(tool, payload):
    decision = decide(tool, payload, WORKTREE)
    assert decision.allow, f"expected allow for {tool} {payload!r}: {decision.reason}"


def test_at_least_20_allow_cases_are_exercised():
    assert len(_ALLOW_CASES) >= 20


# -- ask: table-driven (>= 20 cases) -----------------------------------------

_ASK_CASES = [
    ("Bash", "git push origin main"),
    ("Bash", "git push --force origin main"),
    ("Bash", "gh release create v1.0.0"),
    ("Bash", "gh pr merge 42"),
    ("Bash", "npm publish"),
    ("Bash", "twine upload dist/*"),
    ("Bash", "cargo publish"),
    ("Bash", "docker push myrepo/image:latest"),
    ("Bash", "terraform apply"),
    ("Bash", "kubectl apply -f deploy.yaml"),
    ("Bash", "fly deploy"),
    ("Bash", "vercel deploy --prod"),
    ("Bash", "netlify deploy --prod"),
    ("Bash", "ssh user@remote.example.com 'ls'"),
    ("Bash", "scp file.txt user@remote.example.com:/tmp/"),
    ("Bash", "rsync -av ./dist user@remote.example.com:/var/www"),
    ("Bash", "mail -s 'subject' someone@example.com < body.txt"),
    ("Bash", "sendmail someone@example.com < message.txt"),
    ("Bash", "tg send 'job finished'"),
    ("Bash", "sudo systemctl restart nginx"),
    ("Bash", "sudo apt-get install -y curl"),
    ("Bash", "systemctl restart openclaw-gateway.service"),
    ("Bash", "curl -X POST https://api.example.com/webhook -d '{}'"),
    ("Bash", "curl -X PUT https://api.example.com/resource --data-binary @f.json"),
    ("Bash", "curl -X DELETE https://api.example.com/resource/1"),
    ("Bash", "curl -F file=@report.pdf https://api.example.com/upload"),
    ("Bash", "wget --post-data='x=1' https://api.example.com/submit"),
    ("Bash", "cat ~/.api-keys"),
    ("Bash", "cat $HOME/.api-keys"),
    ("Bash", "cat ${HOME}/.api-keys"),
    ("Bash", 'source "$HOME/.api-keys"'),
    ("Bash", "cat /home/someone/.api-keys"),
    ("Bash", "cd ~ && cat .api-keys"),
    ("Bash", "cat ~/.config/steward/keys.env"),
    ("Bash", "cat $XDG_CONFIG_HOME/steward/keys.env"),
    ("Bash", "cat keys.env"),
    ("Bash", "grep TOKEN '/x/steward/keys.env'"),
    ("Bash", "cat ~/.ssh/id_rsa"),
    ("Bash", "cp ~/.ssh/id_rsa /tmp/key"),
    ("Bash", "cat server.pem"),
    ("Bash", "cat ../secrets/.env"),
    ("Bash", "rm /etc/hosts"),
    ("Bash", "rm -rf /home/user/other-project"),
    ("Bash", f"echo secret > {WORKTREE}/../../etc/passwd"),
    ("Bash", "echo hello > /etc/motd"),
    ("Bash", "echo hi > /home/user/other-project/file.txt"),
    ("Bash", "echo hi > $(curl -s https://evil.example.com/target)"),
    ("Bash", "cat secret.txt > `echo /etc/passwd`"),
    ("Write", {"file_path": "/etc/hosts"}),
    ("Write", {"file_path": "/home/user/other-project/file.py"}),
    ("Edit", {"file_path": "../outside-worktree/file.py"}),
    ("NotebookEdit", {"notebook_path": "/tmp/../../etc/nb.ipynb"}),
]


@pytest.mark.parametrize("tool,payload", _ASK_CASES)
def test_ask_cases(tool, payload):
    decision = decide(tool, payload, WORKTREE)
    assert not decision.allow, f"expected ask for {tool} {payload!r}"
    assert decision.reason


def test_at_least_20_ask_cases_are_exercised():
    assert len(_ASK_CASES) >= 20


def test_redirect_to_outside_path_is_ask():
    decision = decide("Bash", "echo secret >> /home/user/other-project/notes.txt", WORKTREE)
    assert not decision.allow


def test_redirect_to_dev_null_is_allowed():
    for command in ("git add errors.log 2>/dev/null", "make >/dev/null 2>&1", "echo x > /dev/stderr"):
        assert decide("Bash", command, WORKTREE).allow, command
    assert not decide("Bash", "echo x > /dev/sda", WORKTREE).allow


def test_substitution_used_as_redirect_target_is_ask():
    decision = decide("Bash", "echo hi > $(mktemp -p /etc)", WORKTREE)
    assert not decision.allow


def test_default_allow_for_unknown_tool():
    decision = decide("SomeFutureTool", {"anything": "goes"}, WORKTREE)
    assert decision.allow


def test_writes_outside_worktree_are_caught_in_any_segment():
    ask = [
        "cp ticks.csv /home/user/work/steward-smoke/ticks-latest.csv",
        "python3 t.py && cp a /home/user/x",
        "mv /etc/x y",
        "tee /home/user/f < a",
        "touch x; touch /home/user/y",
        "cd /home/user && touch z",
        "dd if=a of=/home/user/img",
        "sed -i s/a/b/ /home/user/c.txt",
        "cp -t /home/user/d a b",
        "install -m 755 tool /usr/local/bin/tool",
        "ln -s build /home/user/link",
    ]
    for command in ask:
        assert not decide("Bash", command, WORKTREE).allow, command
    allow = ["cp a b", "cp a /tmp/b", "echo hi | tee out.txt", "mkdir -p build", "rm -rf build",
             "python3 x.py && cp out.txt results/", "git add errors.log 2>/dev/null"]
    for command in allow:
        assert decide("Bash", command, WORKTREE).allow, command


def test_cwd_tracking_heredocs_and_reads_are_not_false_positives():
    allow = [
        "mkdir -p research && cd research\ncat > 04.md <<'EOF'\n| # | Lane | touch /etc/x |\nEOF\ncd ..",
        "cd /home/user/Documents/Last30Days; cat /home/user/.config/x.json; head -c 9000 f.md",
        "cd /tmp && echo hi > out.txt",
        "cd research; cp a ../b",
        "cat < /etc/hosts",
        "make >/dev/null 2>&1",
    ]
    for command in allow:
        assert decide("Bash", command, WORKTREE).allow, command
    ask = [
        "cd /home/user; echo hi > notes.txt",  # relative write after cd outside
        "cd research; cp a ../../x",
        "echo x > $(pwd)/y",
        "echo x &> /home/user/log",
    ]
    for command in ask:
        assert not decide("Bash", command, WORKTREE).allow, command


def test_data_heredocs_do_not_trip_credential_or_deny_rules():
    # Regression: a mockup written via `cat > f <<EOF` contained ".pem" and was held 24 min.
    data = "mkdir -p /tmp/gen && cat > /tmp/gen/gen.py <<'PYEOF'\nx='ship .pem files'\ny='git push'\nPYEOF"
    assert decide("Bash", data, WORKTREE).allow
    assert not decide("Bash", "cat server.pem", WORKTREE).allow
    assert not decide("Bash", "bash <<'E'\ngit push origin main\nE", WORKTREE).allow  # interpreter heredoc runs
