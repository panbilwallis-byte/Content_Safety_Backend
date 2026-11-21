"""Bounded attack indicators from recorded actions, never executed source text.

A match describes a risky request, not malicious intent or a successful attack.
Quoted examples, file bodies, tool returns and user prompts are not commands.
"""
import json
import posixpath
import re
import shlex


ATTACK_RULES = {
    "credential_access": "敏感凭据读取风险",
    "sensitive_data_exfiltration": "敏感数据外传风险",
    "destructive_command": "破坏性命令风险",
    "remote_code_execution": "远程脚本直接执行风险",
    "reverse_shell": "反向 Shell 风险",
    "security_control_tampering": "安全控制或审计日志篡改风险",
}
SHELL_TOOLS = {"bash", "shell", "exec_command", "run_command", "terminal"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
FILE_READERS = {"cat", "head", "tail", "less", "more", "base64"}


def _unwrap(tokens):
    """Recognize simple wrappers, not arbitrary shell expressions."""
    tokens = list(tokens)
    while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
        tokens.pop(0)
    if tokens and posixpath.basename(tokens[0]) == "sudo":
        tokens.pop(0)
        while tokens and tokens[0] in {"-n", "-E", "--non-interactive"}:
            tokens.pop(0)
    if tokens and tokens[0] == "env":
        tokens.pop(0)
        while tokens and (tokens[0] in {"-i", "--ignore-environment"}
                          or re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0])):
            tokens.pop(0)
    return tokens


def _commands(command, depth=0):
    if not isinstance(command, str) or depth > 2:
        return []
    command = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?^\1\s*$", "", command,
                     flags=re.M | re.S)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.commenters = "#"
        lexer.whitespace_split = True
        groups, tokens, before = [], [], ""
        for token in lexer:
            if token and all(c in ";&|()\n" for c in token):
                if tokens:
                    groups.append((_unwrap(tokens), before))
                tokens, before = [], token
            else:
                tokens.append(token)
        if tokens:
            groups.append((_unwrap(tokens), before))
    except ValueError:
        return []
    result = []
    for tokens, before in groups:
        if not tokens:
            continue
        result.append((tokens, before))
        # A literal shell -c argument is executable source, unlike echo or python
        # string data. Parse it, without evaluating substitutions or variables.
        if posixpath.basename(tokens[0]) in SHELLS:
            for index, token in enumerate(tokens[1:], 1):
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", token) and index + 1 < len(tokens):
                    result.extend(_commands(tokens[index + 1], depth + 1))
                    break
    return result


def _path(value, cwd):
    if not isinstance(value, str) or not value or re.search(r"[$`*?{}]|://", value):
        return None
    if value.startswith(("/", "~/")):
        return posixpath.normpath(value)
    return posixpath.normpath(posixpath.join(cwd, value)) if cwd else None


def _credential(path):
    return bool(path and (path in {"/etc/shadow", "/etc/gshadow"} or re.search(
        r"/(?:\.ssh/id_(?:rsa|dsa|ecdsa|ed25519)|\.aws/credentials|\.kube/config|"
        r"\.config/gcloud/application_default_credentials\.json)$", path)))


def _sensitive(path):
    return bool(path and (_credential(path) or re.search(r"/\.env(?:\.[\w-]+)?$", path)))


def _remote(tokens):
    return any(re.match(r"https?://[^\s]+", token) for token in tokens)


def _upload(tokens):
    """Only curl options that carry a body; a URL mention alone is insufficient."""
    values = []
    for index, token in enumerate(tokens[1:], 1):
        if token in {"-d", "--data", "--data-binary", "--data-urlencode",
                     "-F", "--form", "-T", "--upload-file"}:
            if index + 1 < len(tokens):
                values.append(tokens[index + 1])
        elif token.startswith(("--data=", "--data-binary=", "--data-urlencode=", "--form=", "--upload-file=")):
            values.append(token.split("=", 1)[1])
        elif token.startswith(("-d", "-F", "-T")) and len(token) > 2:
            values.append(token[2:])
    return values


def _download_stdout(tokens):
    program = posixpath.basename(tokens[0])
    destination = None
    for index, token in enumerate(tokens[1:], 1):
        if program == "curl":
            if token in {"-O", "--remote-name"}:
                return False
            if token in {"-o", "--output"} and index + 1 < len(tokens):
                destination = tokens[index + 1]
            elif token.startswith("--output="):
                destination = token.split("=", 1)[1]
            elif token.startswith("-o") and len(token) > 2:
                destination = token[2:]
        elif program == "wget":
            if token in {"-O", "--output-document"} and index + 1 < len(tokens):
                destination = tokens[index + 1]
            elif token.startswith("--output-document="):
                destination = token.split("=", 1)[1]
            else:
                match = re.fullmatch(r"-[a-zA-Z]*O(.+)", token)
                if match:
                    destination = match[1]
    return destination == "-" or (program == "curl" and destination is None)


def _netcat_shell(operands):
    if any(v == "--listen" or re.fullmatch(r"-[a-zA-Z]*l[a-zA-Z]*", v) for v in operands):
        return False
    shell, peers, index = False, [], 0
    while index < len(operands):
        value = operands[index]
        if value in {"-e", "--exec", "-c", "--sh-exec"}:
            index += 1
            if index < len(operands):
                shell |= posixpath.basename(operands[index]) in SHELLS
        elif value in {"-w", "-p", "-s", "-i", "--wait", "--source", "--source-port"}:
            index += 1
        elif not value.startswith("-"):
            peers.append(value)
        index += 1
    return (shell and len(peers) == 2 and bool(re.fullmatch(r"[\w.:-]+", peers[0]))
            and peers[1].isdigit() and 0 < int(peers[1]) <= 65535)


def attack_findings(tool_name, arguments, cwd=None):
    """Return rule IDs and labels. Caller supplies original evidence/coordinates."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return []
    if not isinstance(arguments, dict):
        return []
    name = (tool_name or "").lower()
    found = set()
    cwd = _path(arguments.get("workdir"), cwd) or cwd
    if name == "read":
        if any(_credential(_path(arguments.get(k), cwd)) for k in ("filePath", "file_path", "path")):
            found.add("credential_access")
    if name not in SHELL_TOOLS:
        return [{"rule_id": rule, "label": label} for rule, label in ATTACK_RULES.items() if rule in found]
    commands = _commands(arguments.get("command", arguments.get("cmd", "")))
    previous = []
    for tokens, separator in commands:
        program, operands = posixpath.basename(tokens[0]), tokens[1:]
        paths = [_path(v, cwd) for v in operands if not v.startswith("-")]
        if program in FILE_READERS and any(_credential(p) for p in paths):
            found.add("credential_access")
        if program == "curl" and _remote(operands):
            values = _upload(tokens)
            files = [v.split("@", 1)[1].split(";", 1)[0] for v in values if "@" in v]
            files += [v for v in values if not v.startswith("@")]
            if any(_sensitive(_path(v, cwd)) for v in files):
                found.add("sensitive_data_exfiltration")
            if separator == "|" and previous and posixpath.basename(previous[0]) in FILE_READERS:
                if any(_sensitive(_path(v, cwd)) for v in previous[1:]) and any(
                        v in {"@-", "-"} for v in values):
                    found.add("sensitive_data_exfiltration")
        if program == "scp" and len(operands) >= 2 and re.fullmatch(r"[^/:\s]+@?[^/:\s]*:.+", operands[-1]):
            if any(_sensitive(p) for p in paths[:-1]):
                found.add("sensitive_data_exfiltration")
        flags = [v for v in operands if v.startswith("-")]
        recursive = any(v in {"--recursive"} or re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", v) for v in flags)
        force = any(v == "--force" or re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", v) for v in flags)
        critical = {"/", "/etc", "/home", "/root", "/var", "/var/lib", "/usr", "/boot"}
        if cwd:
            critical.add(posixpath.normpath(cwd))
        if program == "rm" and recursive and force and any(p in critical for p in paths):
            found.add("destructive_command")
        if (program == "dd" and any(re.fullmatch(r"of=/dev/(?:sd[a-z]|vd[a-z]|nvme\d+n\d+)(?:p?\d+)?", v) for v in operands)
                or (program == "mkfs" or program.startswith("mkfs.")) and any(
                    p and re.fullmatch(r"/dev/(?:sd[a-z]|vd[a-z]|nvme\d+n\d+)(?:p?\d+)?", p) for p in paths)):
            found.add("destructive_command")
        if program in SHELLS and separator == "|" and previous:
            if posixpath.basename(previous[0]) in {"curl", "wget"} and _remote(previous[1:]):
                # Do not call download-to-file pipelines execution of remote bytes.
                shell_reads_stdin = not any(not v.startswith("-") or re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", v)
                                           for v in operands)
                if _download_stdout(previous) and shell_reads_stdin:
                    found.add("remote_code_execution")
        if program in SHELLS and any(re.fullmatch(r"/dev/tcp/[^/\s]+/\d+", p or "") for p in paths):
            if any("i" in flag for flag in flags):
                found.add("reverse_shell")
        # Shell redirections may be attached to tokens (e.g. >&/dev/tcp/...).
        if program in SHELLS and any(re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z]*", f) for f in flags):
            if any(re.search(r"(?:^|[>&])/dev/tcp/[^/\s]+/\d+(?:$|\s)", v) for v in operands):
                found.add("reverse_shell")
        if program in {"nc", "netcat", "ncat"} and _netcat_shell(operands):
            found.add("reverse_shell")
        if program == "socat" and any(v.startswith(("TCP:", "TCP4:", "TCP6:")) for v in operands):
            if any(re.match(r"(?:EXEC|SYSTEM):(?:/bin/)?(?:ba)?sh(?:,|$)", v) for v in operands):
                found.add("reverse_shell")
        if (program == "setenforce" and operands == ["0"]
                or program == "ufw" and "disable" in operands
                or program == "auditctl" and any(operands[i:i + 2] == ["-e", "0"] for i in range(len(operands) - 1))
                or program == "systemctl" and any(v in {"stop", "disable", "mask"} for v in operands)
                and any(v.removesuffix(".service") in {"auditd", "firewalld", "apparmor"} for v in operands)):
            found.add("security_control_tampering")
        if program in {"rm", "truncate"} and any(p and (p in {"/var/log/audit", "/var/log/audit/audit.log", "/var/log/auth.log", "/var/log/secure"}) for p in paths):
            if program == "rm" or any(v == "--size=0" or v == "-s0" or v == "-s" and i + 1 < len(tokens)
                                       and tokens[i + 1] == "0" for i, v in enumerate(tokens)):
                found.add("security_control_tampering")
        if program == "cd" and operands:
            cwd = _path(operands[0], cwd)
        previous = tokens
    return [{"rule_id": rule, "label": label} for rule, label in ATTACK_RULES.items() if rule in found]
