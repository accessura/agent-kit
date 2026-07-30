#!/usr/bin/env python3
"""Accessura CLI — scaffold MCP server projects.

Usage:
    accessura init my-agent
    accessura init my-agent --role seller
    accessura init my-agent --network testnet  # default

Learnings from Beep SDK (@beep-it/cli):
    beep init my-server --transport=http
    https://github.com/beep-it/beep-sdk
"""

import argparse
import os
import shutil
import sys
from pathlib import Path


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "default"

DEFAULT_ENV = """# Accessura MCP Server — credentials (never share these files)
ACCESSURA_BASE_URL=https://testnet.accessura.io
ACCESSURA_API_KEY=acc_
ACCESSURA_PRIVATE_KEY=0x
ACCESSURA_DELIVERY_SECRET=
"""

SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""{project_name} — Accessura MCP Server

Scaffolded with `accessura init`. Requires agent-kit to be installed:

    pip install "accessura-agent-kit @ git+https://github.com/accessura/agent-kit.git"

Then:
    cp .env.example .env   # fill in your credentials
    python mcp-server.py     # stdio (Claude Code)

The agent-kit ships with 20+ built-in MCP tools (discovery, bidding,
settlement, claims, payment, delivery, decrypt, auth, seller payout).
"""

import os
import sys

from dotenv import load_dotenv
load_dotenv()

# Ensure the env file was copied and edited
if os.getenv("ACCESSURA_PRIVATE_KEY", "").startswith("0x") and len(os.getenv("ACCESSURA_PRIVATE_KEY", "")) > 32:
    print(f"[{project_name}] Credentials loaded from .env", file=sys.stderr)
else:
    print(f"[{project_name}] WARNING: ACCESSURA_PRIVATE_KEY not set. Run auth_register + auth_apikey first, or set ACCESSURA_API_KEY.", file=sys.stderr)

# Run the agent-kit MCP server (all 20+ built-in tools are registered automatically)
from server import main as server_main
sys.exit(server_main())
'''


README_TEMPLATE = """# {project_name}

Scaffolded with `accessura init`.

## Setup

```bash
pip install "accessura-agent-kit @ git+https://github.com/accessura/agent-kit.git"
cp .env.example .env
# Edit .env with your credentials:
#   ACCESSURA_PRIVATE_KEY = your secp256k1 wallet private key (0x-prefixed hex)
#   ACCESSURA_DELIVERY_SECRET = (seller only) dedicated 32-byte hex secret
```

## Run

```bash
# Stdio (Claude Code)
python mcp-server.py

# HTTP (remote / web)
python mcp-server.py --http 3000
```

## Register with Claude Code

```bash
claude mcp add {project_name} -- python mcp-server.py
```

Or `.mcp.json`:

```json
{{
  "mcpServers": {{
    "accessura": {{
      "command": "python",
      "args": ["mcp-server.py"],
      "env": {{
        "ACCESSURA_BASE_URL": "https://testnet.accessura.io",
        "ACCESSURA_API_KEY": "acc_...",
        "ACCESSURA_PRIVATE_KEY": "0x..."
      }}
    }}
  }}
}}
```

## Project structure

- `mcp-server.py` — the MCP server entry point
- `.env` — your credentials (never commit this file)
- `.env.example` — template for credentials

## Next steps

1. Register your agent: run `auth_register` + `auth_apikey` tools (or call via Claude Code)
2. {next_step}
3. See [references/](https://github.com/accessura/agent-kit/tree/main/accessura/references) for the full API contract
"""

BUYER_NEXT_STEP = "Discover topics: `topics_list`, then find packs and place bids"
SELLER_NEXT_STEP = "Bind your payout wallet: `seller_payout_bind`, then publish packs"
BOTH_NEXT_STEP = "Start as a buyer: `topics_list` to discover markets, `bids_place` to bid. Or as a seller: `seller_payout_bind` then `packs_publish`."


def scaffold(target_dir: Path, project_name: str, role: str) -> None:
    if target_dir.exists():
        print(f"Error: {target_dir} already exists.", file=sys.stderr)
        sys.exit(1)

    target_dir.mkdir(parents=True)

    # .env.example
    (target_dir / ".env.example").write_text(DEFAULT_ENV)

    # mcp-server.py
    server_content = SERVER_TEMPLATE.format(project_name=project_name)
    (target_dir / "mcp-server.py").write_text(server_content)
    (target_dir / "mcp-server.py").chmod(0o755)

    # README.md
    next_step = {
        "buyer": BUYER_NEXT_STEP,
        "seller": SELLER_NEXT_STEP,
        "both": BOTH_NEXT_STEP,
    }.get(role, BOTH_NEXT_STEP)
    readme_content = README_TEMPLATE.format(
        project_name=project_name, next_step=next_step
    )
    (target_dir / "README.md").write_text(readme_content)

    files_created = 3
    print(f"Created {project_name}/ ({files_created} files)")
    print(f"  .env.example")
    print(f"  mcp-server.py")
    print(f"  README.md")
    print()
    print("Next steps:")
    print(f"  cd {project_name}")
    print(f"  cp .env.example .env")
    print(f"  # edit .env with your credentials")
    print(f"  pip install \"accessura-agent-kit @ git+https://github.com/accessura/agent-kit.git\"")
    print(f"  python mcp-server.py")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="accessura",
        description="Accessura CLI — scaffold MCP server projects",
    )
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Scaffold a new MCP server project")
    init.add_argument("name", help="Project name / directory")
    init.add_argument(
        "--role",
        choices=["buyer", "seller", "both"],
        default="both",
        help="Agent role (default: both)",
    )

    args = parser.parse_args()
    if args.command == "init":
        target = Path(args.name).resolve()
        scaffold(target, args.name, args.role)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
