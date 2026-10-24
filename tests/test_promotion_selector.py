"""Run the actual promotion selector without a repository checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_selector_downloads_proof_without_a_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/deploy-prod.yml").read_text()
    script = dedent(workflow.split("        run: |\n", 1)[1].split("\n  promote:", 1)[0])
    script = script.replace("${{ inputs.confirm_production }}", "true")
    release = {
        "image_tag": "20261024-043838-c94d09e",
        "source_sha": "c94d09ec50c4e0fd6f2a596c95ea9351d513c324",
        "dev_run_id": "40062815554",
    }
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "release = json.loads(os.environ['TEST_RELEASE'])\n"
        "if args[0] == 'api':\n"
        "    print(json.dumps([{'id': release['dev_run_id'], 'head_sha': release['source_sha']}]))\n"
        "elif args[:2] == ['run', 'download']:\n"
        "    assert '--repo' in args, 'artifact download requires a repository without a checkout'\n"
        "    assert args[args.index('--repo') + 1] == os.environ['GITHUB_REPOSITORY']\n"
        "    dest = Path(args[args.index('-D') + 1])\n"
        "    (dest / 'dev-release.json').write_text(json.dumps(release))\n"
        "else:\n"
        "    raise AssertionError(args)\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "DataTalksClub/relay",
             "GITHUB_OUTPUT": str(output), "IMAGE_TAG_INPUT": release["image_tag"],
             "TEST_RELEASE": json.dumps(release)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Promoting dev-proven release:" in result.stdout
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == release
