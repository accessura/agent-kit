import importlib.util
import io
import json
import stat
from pathlib import Path

from eth_account import Account


def load_helper():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "prepare_funded_testnet_env.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_prepare_funded_testnet_env", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPER = load_helper()
BUYER_KEY = "0x" + "11" * 32
SELLER_KEY = "0x" + "22" * 32
DELIVERY_SECRET = "33" * 32
BUYER = Account.from_key(BUYER_KEY).address
SELLER = Account.from_key(SELLER_KEY).address


def funded_env(**overrides):
    values = {
        "ACCESSURA_FUNDED_BUYER_PRIVATE_KEY": BUYER_KEY,
        "ACCESSURA_FUNDED_SELLER_PRIVATE_KEY": SELLER_KEY,
        "ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS": SELLER,
        "ACCESSURA_DELIVERY_SECRET": DELIVERY_SECRET,
        "ACCESSURA_BASE_SEPOLIA_RPC_URL": "https://rpc.invalid.example",
    }
    values.update(overrides)
    return values


def abi_string(value):
    encoded = value.encode("ascii")
    padding = b"\x00" * ((32 - len(encoded) % 32) % 32)
    return "0x" + (
        (32).to_bytes(32, "big")
        + len(encoded).to_bytes(32, "big")
        + encoded
        + padding
    ).hex()


class FakeRpc:
    methods = []
    balance = 20_000
    chain_id = HELPER.BASE_SEPOLIA_CHAIN_ID

    def __init__(self, url):
        assert url == "https://rpc.invalid.example"

    def call(self, method, params):
        self.methods.append(method)
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getCode":
            assert params == [HELPER.BASE_SEPOLIA_USDC, "latest"]
            return "0x60006000"
        assert method == "eth_call"
        call = params[0]
        assert call["to"] == HELPER.BASE_SEPOLIA_USDC
        data = call["data"]
        if data == f"0x{HELPER.DECIMALS_SELECTOR}":
            return hex(HELPER.USDC_DECIMALS)
        if data == f"0x{HELPER.SYMBOL_SELECTOR}":
            return abi_string("USDC")
        if data.startswith(f"0x{HELPER.BALANCE_OF_SELECTOR}"):
            assert data.endswith(BUYER[2:].lower())
            return hex(self.balance)
        raise AssertionError(f"unexpected eth_call: {data}")


def run_check(values, rpc_factory=FakeRpc):
    stream = io.StringIO()
    reporter = HELPER.CheckReporter(stream)
    result = HELPER.run_checks(values, rpc_factory, reporter)
    return result, stream.getvalue()


def test_check_uses_only_read_only_rpc_and_never_prints_credentials():
    FakeRpc.methods = []
    FakeRpc.balance = 20_000
    FakeRpc.chain_id = HELPER.BASE_SEPOLIA_CHAIN_ID

    result, output = run_check(funded_env())

    assert result == 0
    assert set(FakeRpc.methods) == {"eth_chainId", "eth_getCode", "eth_call"}
    assert BUYER in output and SELLER in output
    assert BUYER_KEY not in output
    assert SELLER_KEY not in output
    assert DELIVERY_SECRET not in output
    assert "no testnet.accessura.io pre-registration" in output
    assert "neither Buyer nor Seller needs Base Sepolia ETH" in output
    assert HELPER.BASE_SEPOLIA_USDC in output


def test_check_rejects_wrong_chain_before_token_or_balance_acceptance():
    FakeRpc.methods = []
    FakeRpc.chain_id = 1

    result, output = run_check(funded_env())

    assert result == 1
    assert "FAIL Base Sepolia RPC: RPC reports chainId 1" in output
    assert "FAIL USDC contract" in output
    assert "FAIL Buyer USDC balance" in output
    assert "eth_getCode" not in FakeRpc.methods


def test_check_reports_usdc_shortfall_and_official_faucet():
    FakeRpc.methods = []
    FakeRpc.chain_id = HELPER.BASE_SEPOLIA_CHAIN_ID
    FakeRpc.balance = 5_000

    result, output = run_check(funded_env())

    assert result == 1
    assert "short 0.006000 USDC" in output
    assert HELPER.CIRCLE_TESTNET_FAUCET in output
    assert "selecting Base Sepolia USDC" in output


def test_check_rejects_wrong_token_metadata_at_the_fixed_address():
    class WrongTokenRpc(FakeRpc):
        def call(self, method, params):
            if (
                method == "eth_call"
                and params[0]["data"] == f"0x{HELPER.SYMBOL_SELECTOR}"
            ):
                return abi_string("FAKE")
            return super().call(method, params)

    FakeRpc.chain_id = HELPER.BASE_SEPOLIA_CHAIN_ID
    result, output = run_check(funded_env(), WrongTokenRpc)

    assert result == 1
    assert "FAIL USDC contract" in output
    assert "symbol='FAKE'" in output
    assert "FAIL Buyer USDC balance" in output


def test_check_rejects_reused_delivery_secret_and_any_mainnet_variable():
    result, output = run_check(
        funded_env(
            ACCESSURA_DELIVERY_SECRET=SELLER_KEY,
            ACCESSURA_ALLOW_MAINNET="0",
        )
    )

    assert result == 1
    assert "FAIL delivery secret" in output
    assert "equals the Seller private key" in output
    assert "FAIL mainnet exclusion" in output


def test_check_rejects_payout_not_controlled_by_the_seller_key():
    result, output = run_check(
        funded_env(ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS=BUYER)
    )

    assert result == 1
    assert f"public payout {BUYER} does not match Seller {SELLER}" in output


def test_helper_cli_has_no_credential_arguments():
    option_strings = {
        option
        for action in HELPER.build_parser()._actions
        for option in action.option_strings
    }
    assert option_strings == {
        "-h",
        "--help",
        "--check",
        "--write-env",
        "--out",
    }
    assert not any(
        token in option.lower()
        for option in option_strings
        for token in ("key", "token", "secret", "payout")
    )


def test_rpc_client_sets_public_json_headers(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": "0x14a34"}
            ).encode()

    def fake_urlopen(request, timeout):
        observed["headers"] = dict(request.header_items())
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(HELPER.urllib.request, "urlopen", fake_urlopen)
    rpc = HELPER.ReadOnlyRpcClient("https://rpc.invalid.example")

    assert rpc.call("eth_chainId", []) == "0x14a34"
    assert observed["headers"]["Accept"] == "application/json"
    assert observed["headers"]["Content-type"] == "application/json"
    assert observed["headers"]["User-agent"].startswith(
        "Accessura-Agent-Kit-Funded-Preflight/"
    )
    assert observed["timeout"] == 20


def test_write_env_copies_only_existing_allowlist_values_with_mode_0600(tmp_path):
    path = tmp_path / ".funded-env"
    values = funded_env(
        ACCESSURA_FUNDED_BID_USDC="0.02",
        NOT_ALLOWLISTED="must-not-be-written",
    )
    stream = io.StringIO()
    result = HELPER.write_env(
        path,
        values,
        HELPER.CheckReporter(stream),
    )
    content = path.read_text(encoding="utf-8")

    assert result == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f"export ACCESSURA_FUNDED_BUYER_PRIVATE_KEY='{BUYER_KEY}'" in content
    assert f"export ACCESSURA_FUNDED_SELLER_PRIVATE_KEY='{SELLER_KEY}'" in content
    assert f"export ACCESSURA_DELIVERY_SECRET='{DELIVERY_SECRET}'" in content
    assert "export ACCESSURA_FUNDED_BID_USDC='0.02'" in content
    assert "NOT_ALLOWLISTED" not in content
    assert "ACCESSURA_ALLOW_MAINNET" not in content
    assert BUYER_KEY not in stream.getvalue()
    assert SELLER_KEY not in stream.getvalue()
    assert DELIVERY_SECRET not in stream.getvalue()


def test_write_env_refuses_mainnet_override_without_creating_file(tmp_path):
    path = tmp_path / ".funded-env"
    stream = io.StringIO()

    result = HELPER.write_env(
        path,
        funded_env(ACCESSURA_ALLOW_MAINNET="1"),
        HELPER.CheckReporter(stream),
    )

    assert result == 1
    assert not path.exists()
    assert "FAIL mainnet exclusion" in stream.getvalue()
