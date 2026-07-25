#!/usr/bin/env python3
"""Validate and assemble the local funded Base Sepolia environment.

This helper never creates a wallet, signs a message, submits a transaction, or
calls an Accessura API. Private keys and the delivery secret are accepted only
through the process environment and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO
from urllib.parse import urlsplit


BASE_SEPOLIA_CHAIN_ID = 84532
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6
DEFAULT_BID_USDC = Decimal("0.01")
USDC_BALANCE_BUFFER = Decimal("0.001")
CIRCLE_TESTNET_FAUCET = "https://faucet.circle.com/"

REQUIRED_ENV_NAMES = (
    "ACCESSURA_FUNDED_BUYER_PRIVATE_KEY",
    "ACCESSURA_FUNDED_SELLER_PRIVATE_KEY",
    "ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS",
    "ACCESSURA_DELIVERY_SECRET",
    "ACCESSURA_BASE_SEPOLIA_RPC_URL",
)
OPTIONAL_ENV_NAMES = (
    "ACCESSURA_FUNDED_BASE_URL",
    "ACCESSURA_FUNDED_TOPIC_SLUG",
    "ACCESSURA_FUNDED_BID_USDC",
    "ACCESSURA_FUNDED_WINDOW_SECONDS",
    "ACCESSURA_FUNDED_SETTLE_TIMEOUT_SECONDS",
    "ACCESSURA_FUNDED_POLL_SECONDS",
    "ACCESSURA_PLATFORM_ADDRESSES",
    "ACCESSURA_MAX_PAY_USDC",
)

PRIVATE_KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
HEX_32_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

BALANCE_OF_SELECTOR = "70a08231"
DECIMALS_SELECTOR = "313ce567"
SYMBOL_SELECTOR = "95d89b41"


class PreparationError(RuntimeError):
    """A safe, credential-free preparation failure."""


class CheckReporter:
    """Print one public PASS/FAIL line per local or read-only check."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        import sys

        self.stream = stream or sys.stdout
        self.failures = 0

    def passed(self, label: str, detail: str) -> None:
        print(f"PASS {label}: {detail}", file=self.stream)

    def failed(self, label: str, detail: str) -> None:
        self.failures += 1
        print(f"FAIL {label}: {detail}", file=self.stream)


class ReadOnlyRpcClient:
    """Small JSON-RPC client that permits only the checks used here."""

    ALLOWED_METHODS = frozenset({"eth_chainId", "eth_getCode", "eth_call"})

    def __init__(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PreparationError(
                "ACCESSURA_BASE_SEPOLIA_RPC_URL must be an HTTP(S) URL"
            )
        self._url = url
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise PreparationError(
                f"RPC method {method} is not permitted by this read-only helper"
            )
        self._request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Accessura-Agent-Kit-Funded-Preflight/0.6",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PreparationError(
                f"Base Sepolia RPC returned HTTP {exc.code}"
            ) from None
        except (
            OSError,
            urllib.error.URLError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise PreparationError(
                "Base Sepolia RPC could not be read; check the local RPC URL"
            ) from None
        if not isinstance(payload, dict) or payload.get("error") is not None:
            raise PreparationError(
                f"Base Sepolia RPC rejected read-only method {method}"
            )
        if "result" not in payload:
            raise PreparationError(
                f"Base Sepolia RPC omitted the {method} result"
            )
        return payload["result"]


def _normalized_hex_bytes(value: str) -> Optional[bytes]:
    if not HEX_32_RE.fullmatch(value):
        return None
    normalized = value[2:] if value.lower().startswith("0x") else value
    return bytes.fromhex(normalized)


def _derive_address(private_key: str) -> Optional[str]:
    if not PRIVATE_KEY_RE.fullmatch(private_key):
        return None
    try:
        from eth_account import Account

        return str(Account.from_key(private_key).address)
    except (ImportError, TypeError, ValueError):
        return None


def _decimal_env(
    environ: Mapping[str, str],
    name: str,
    default: Decimal,
) -> Optional[Decimal]:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    scaled = value * (10 ** USDC_DECIMALS)
    if scaled != scaled.to_integral_value():
        return None
    return value


def _usdc_base_units(value: Decimal) -> int:
    return int(value * (10 ** USDC_DECIMALS))


def _format_usdc(base_units: int) -> str:
    return f"{Decimal(base_units) / (10 ** USDC_DECIMALS):.6f}"


def _eth_call(rpc: ReadOnlyRpcClient, data: str) -> str:
    result = rpc.call(
        "eth_call",
        [{"to": BASE_SEPOLIA_USDC, "data": f"0x{data}"}, "latest"],
    )
    if not isinstance(result, str) or not result.startswith("0x"):
        raise PreparationError("Base Sepolia USDC eth_call returned invalid data")
    return result


def _decode_uint256(value: str, label: str) -> int:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        raise PreparationError(
            f"Base Sepolia USDC {label} returned invalid ABI data"
        ) from None


def _decode_abi_string(value: str) -> str:
    try:
        raw = bytes.fromhex(value[2:])
        if len(raw) == 32:
            return raw.rstrip(b"\x00").decode("ascii")
        if len(raw) < 64:
            raise ValueError
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 > len(raw):
            raise ValueError
        length = int.from_bytes(raw[offset : offset + 32], "big")
        start = offset + 32
        end = start + length
        if end > len(raw):
            raise ValueError
        return raw[start:end].decode("ascii")
    except (UnicodeDecodeError, ValueError):
        raise PreparationError(
            "Base Sepolia USDC symbol returned invalid ABI data"
        ) from None


def _print_execution_contract(reporter: CheckReporter) -> None:
    reporter.passed(
        "identity setup",
        (
            "no testnet.accessura.io pre-registration or payout pre-bind is "
            "required; verify_funded_testnet_evidence.py --execute calls "
            "register/auth for Buyer and Seller and bind_payout_wallet for Seller"
        ),
    )
    reporter.passed(
        "gas requirement",
        (
            "neither Buyer nor Seller needs Base Sepolia ETH in the current "
            "execute flow: Buyer signs EIP-3009 locally, the hosted facilitator "
            "submits the payment, and Seller setup uses off-chain signatures; "
            "no ETH balance query was needed"
        ),
    )


def run_checks(
    environ: Optional[Mapping[str, str]] = None,
    rpc_factory: Callable[[str], ReadOnlyRpcClient] = ReadOnlyRpcClient,
    reporter: Optional[CheckReporter] = None,
) -> int:
    """Run local consistency checks plus read-only Base Sepolia RPC calls."""
    values = os.environ if environ is None else environ
    output = reporter or CheckReporter()

    if "ACCESSURA_ALLOW_MAINNET" in values:
        output.failed(
            "mainnet exclusion",
            "ACCESSURA_ALLOW_MAINNET must be absent, even when its value is 0",
        )
    else:
        output.passed(
            "mainnet exclusion",
            "ACCESSURA_ALLOW_MAINNET is absent; Base mainnet stays disabled",
        )

    missing = [name for name in REQUIRED_ENV_NAMES if not values.get(name)]
    if missing:
        output.failed(
            "required environment",
            f"missing {', '.join(missing)}",
        )
    else:
        output.passed(
            "required environment",
            "all five runbook variables are present in this process",
        )

    buyer_key = values.get("ACCESSURA_FUNDED_BUYER_PRIVATE_KEY", "")
    seller_key = values.get("ACCESSURA_FUNDED_SELLER_PRIVATE_KEY", "")
    buyer_address = _derive_address(buyer_key)
    seller_address = _derive_address(seller_key)

    if buyer_address:
        output.passed("Buyer key", f"derives public address {buyer_address}")
    else:
        output.failed(
            "Buyer key",
            "ACCESSURA_FUNDED_BUYER_PRIVATE_KEY is not a valid 32-byte EVM key",
        )
    if seller_address:
        output.passed("Seller key", f"derives public address {seller_address}")
    else:
        output.failed(
            "Seller key",
            "ACCESSURA_FUNDED_SELLER_PRIVATE_KEY is not a valid 32-byte EVM key",
        )

    if buyer_address and seller_address:
        if buyer_address.lower() != seller_address.lower():
            output.passed(
                "identity separation",
                "Buyer and Seller public addresses are different",
            )
        else:
            output.failed(
                "identity separation",
                "Buyer and Seller derive the same public address",
            )
    else:
        output.failed(
            "identity separation",
            "cannot compare until both private keys are valid",
        )

    payout = values.get("ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS", "")
    if not ADDRESS_RE.fullmatch(payout):
        output.failed(
            "Seller payout",
            "ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS is not an EVM address",
        )
    elif not seller_address:
        output.failed(
            "Seller payout",
            "cannot verify control until the Seller private key is valid",
        )
    elif payout.lower() != seller_address.lower():
        output.failed(
            "Seller payout",
            f"public payout {payout} does not match Seller {seller_address}",
        )
    else:
        output.passed(
            "Seller payout",
            f"public payout {payout} matches the Seller-derived address",
        )

    secret_bytes = _normalized_hex_bytes(
        values.get("ACCESSURA_DELIVERY_SECRET", "")
    )
    seller_key_bytes = _normalized_hex_bytes(seller_key)
    if secret_bytes is None:
        output.failed(
            "delivery secret",
            "ACCESSURA_DELIVERY_SECRET must be exactly 32 bytes of hex",
        )
    elif seller_key_bytes is None:
        output.failed(
            "delivery secret",
            "format is valid, but separation cannot be checked until the Seller key is valid",
        )
    elif secret_bytes == seller_key_bytes:
        output.failed(
            "delivery secret",
            "ACCESSURA_DELIVERY_SECRET equals the Seller private key",
        )
    else:
        output.passed(
            "delivery secret",
            "it is 32-byte hex and differs from the Seller private key",
        )

    base_url = values.get(
        "ACCESSURA_FUNDED_BASE_URL", "https://testnet.accessura.io"
    ).rstrip("/")
    if base_url == "https://testnet.accessura.io":
        output.passed(
            "Accessura target",
            "execute will use https://testnet.accessura.io",
        )
    else:
        output.failed(
            "Accessura target",
            "ACCESSURA_FUNDED_BASE_URL must be https://testnet.accessura.io",
        )

    bid = _decimal_env(
        values,
        "ACCESSURA_FUNDED_BID_USDC",
        DEFAULT_BID_USDC,
    )
    if bid is None:
        output.failed(
            "bid amount",
            "ACCESSURA_FUNDED_BID_USDC must be positive with at most 6 decimals",
        )
    else:
        output.passed(
            "bid amount",
            f"{bid:.6f} USDC plus a fixed {USDC_BALANCE_BUFFER:.6f} USDC buffer",
        )

    max_pay = _decimal_env(
        values,
        "ACCESSURA_MAX_PAY_USDC",
        Decimal("100"),
    )
    if max_pay is None:
        output.failed(
            "payment ceiling",
            "ACCESSURA_MAX_PAY_USDC must be positive with at most 6 decimals",
        )
    elif bid is not None and max_pay < bid:
        output.failed(
            "payment ceiling",
            f"{max_pay:.6f} USDC is lower than the {bid:.6f} USDC bid",
        )
    else:
        output.passed(
            "payment ceiling",
            f"{max_pay:.6f} USDC permits the configured bid",
        )

    rpc_url = values.get("ACCESSURA_BASE_SEPOLIA_RPC_URL", "")
    rpc: Optional[ReadOnlyRpcClient] = None
    chain_ok = False
    if not rpc_url:
        output.failed(
            "Base Sepolia RPC",
            "ACCESSURA_BASE_SEPOLIA_RPC_URL is missing",
        )
    else:
        try:
            rpc = rpc_factory(rpc_url)
            chain_id = _decode_uint256(
                rpc.call("eth_chainId", []),
                "chain ID",
            )
            if chain_id != BASE_SEPOLIA_CHAIN_ID:
                output.failed(
                    "Base Sepolia RPC",
                    f"RPC reports chainId {chain_id}, expected {BASE_SEPOLIA_CHAIN_ID}",
                )
            else:
                chain_ok = True
                output.passed(
                    "Base Sepolia RPC",
                    f"RPC reports chainId {BASE_SEPOLIA_CHAIN_ID}",
                )
        except PreparationError as exc:
            output.failed("Base Sepolia RPC", str(exc))

    contract_ok = False
    if not rpc or not chain_ok:
        output.failed(
            "USDC contract",
            (
                f"cannot verify fixed Base Sepolia USDC {BASE_SEPOLIA_USDC} "
                "until the chain check passes"
            ),
        )
    else:
        try:
            code = rpc.call("eth_getCode", [BASE_SEPOLIA_USDC, "latest"])
            if not isinstance(code, str) or code.lower() in {"", "0x", "0x0"}:
                output.failed(
                    "USDC contract",
                    f"{BASE_SEPOLIA_USDC} has no bytecode on this RPC",
                )
            else:
                decimals = _decode_uint256(
                    _eth_call(rpc, DECIMALS_SELECTOR),
                    "decimals",
                )
                symbol = _decode_abi_string(_eth_call(rpc, SYMBOL_SELECTOR))
                if decimals != USDC_DECIMALS or symbol != "USDC":
                    output.failed(
                        "USDC contract",
                        (
                            f"{BASE_SEPOLIA_USDC} returned symbol={symbol!r}, "
                            f"decimals={decimals}; expected USDC/6"
                        ),
                    )
                else:
                    contract_ok = True
                    output.passed(
                        "USDC contract",
                        (
                            f"{BASE_SEPOLIA_USDC} has bytecode and reports "
                            "symbol=USDC, decimals=6; other testnet USDC or "
                            "other-chain tokens are not accepted"
                        ),
                    )
        except PreparationError as exc:
            output.failed("USDC contract", str(exc))

    if not rpc or not chain_ok or not contract_ok:
        output.failed(
            "Buyer USDC balance",
            "cannot read the fixed Base Sepolia USDC balance until chain and contract checks pass",
        )
    elif not buyer_address:
        output.failed(
            "Buyer USDC balance",
            "cannot read balance until the Buyer key derives a valid public address",
        )
    elif bid is None:
        output.failed(
            "Buyer USDC balance",
            "cannot calculate the threshold until the bid amount is valid",
        )
    else:
        try:
            padded_address = buyer_address[2:].lower().rjust(64, "0")
            balance = _decode_uint256(
                _eth_call(rpc, f"{BALANCE_OF_SELECTOR}{padded_address}"),
                "balanceOf",
            )
            required = (
                _usdc_base_units(bid)
                + _usdc_base_units(USDC_BALANCE_BUFFER)
            )
            if balance >= required:
                output.passed(
                    "Buyer USDC balance",
                    (
                        f"{_format_usdc(balance)} USDC at {buyer_address}; "
                        f"required >= {_format_usdc(required)} USDC"
                    ),
                )
            else:
                shortfall = required - balance
                output.failed(
                    "Buyer USDC balance",
                    (
                        f"{_format_usdc(balance)} USDC at {buyer_address}; "
                        f"short {_format_usdc(shortfall)} USDC against the "
                        f"{_format_usdc(required)} USDC threshold. Refill from "
                        f"{CIRCLE_TESTNET_FAUCET} by selecting Base Sepolia "
                        f"USDC for {BASE_SEPOLIA_USDC}"
                    ),
                )
        except PreparationError as exc:
            output.failed("Buyer USDC balance", str(exc))

    _print_execution_contract(output)
    return 1 if output.failures else 0


def _shell_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise PreparationError(
            "environment values containing newlines cannot be written"
        )
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _write_private_env_file(path: Path, content: str) -> None:
    target = path.expanduser()
    parent = target.parent
    if not parent.exists() or not parent.is_dir():
        raise PreparationError(f"output directory does not exist: {parent}")
    if target.is_symlink():
        raise PreparationError("refusing to replace a symlink output path")

    previous_umask = os.umask(0o077)
    temporary_name: Optional[str] = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=str(parent),
            text=True,
        )
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    finally:
        os.umask(previous_umask)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def write_env(
    output_path: Path,
    environ: Optional[Mapping[str, str]] = None,
    reporter: Optional[CheckReporter] = None,
) -> int:
    """Copy only allowlisted, already-exported values into a private env file."""
    values = os.environ if environ is None else environ
    output = reporter or CheckReporter()

    if "ACCESSURA_ALLOW_MAINNET" in values:
        output.failed(
            "mainnet exclusion",
            "ACCESSURA_ALLOW_MAINNET must be absent; no env file was written",
        )
        return 1

    missing = [name for name in REQUIRED_ENV_NAMES if not values.get(name)]
    if missing:
        output.failed(
            "required environment",
            f"missing {', '.join(missing)}; no env file was written",
        )
        return 1

    names = list(REQUIRED_ENV_NAMES)
    names.extend(name for name in OPTIONAL_ENV_NAMES if name in values)
    try:
        lines = [
            "# Contains local funded-Testnet credentials; never commit or share.",
            "# Values were copied from this process; this helper generated none.",
        ]
        lines.extend(
            f"export {name}={_shell_quote(values[name])}" for name in names
        )
        lines.append("")
        _write_private_env_file(output_path, "\n".join(lines))
    except (OSError, PreparationError) as exc:
        output.failed("env file", str(exc))
        return 1

    mode = stat.S_IMODE(output_path.expanduser().stat().st_mode)
    output.passed(
        "env file",
        (
            f"wrote {output_path.expanduser().resolve()} with mode {mode:04o}; "
            "credential values were not printed"
        ),
    )
    output.passed(
        "mainnet exclusion",
        "ACCESSURA_ALLOW_MAINNET was not copied and remains forbidden",
    )
    _print_execution_contract(output)
    output.passed(
        "USDC requirement",
        (
            f"the funded run accepts only Base Sepolia USDC "
            f"{BASE_SEPOLIA_USDC}; run --check before execute"
        ),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Locally validate or assemble the environment for the funded "
            "Base Sepolia release check; never creates a wallet or moves funds"
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="run local consistency and read-only Base Sepolia RPC checks",
    )
    mode.add_argument(
        "--write-env",
        action="store_true",
        help="copy already-exported allowlisted values to a mode-0600 env file",
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help="output for --write-env (default: .funded-env)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.check:
        if args.out is not None:
            parser.error("--out can only be used with --write-env")
        return run_checks()
    return write_env(args.out or Path(".funded-env"))


if __name__ == "__main__":
    raise SystemExit(main())
