import functools
import json
import logging
from pathlib import Path
from types import MethodType
from typing import List, Optional, Union, cast

import toml
from eth_abi.exceptions import InsufficientDataBytes
from eth_account import Account as EvmAccount
from eth_account._utils.typed_transactions import TypedTransaction
from eth_keys import keys
from eth_utils.address import to_checksum_address
from hexbytes import HexBytes
from starknet_py.net.account.account import Account
from starknet_py.net.client_errors import ClientError
from starknet_py.net.client_models import Call, Event
from starknet_py.net.signer.stark_curve_signer import KeyPair
from starkware.starknet.public.abi import starknet_keccak
from web3 import Web3
from web3._utils.abi import map_abi_data
from web3._utils.events import get_event_data
from web3._utils.normalizers import BASE_RETURN_NORMALIZERS
from web3.contract import Contract as Web3Contract
from web3.contract.contract import ContractEvents
from web3.exceptions import LogTopicError, MismatchedABI, NoABIFunctionsFound
from web3.types import LogReceipt

from scripts.artifacts import fetch_deployments
from scripts.constants import (
    CLIENT,
    EVM_ADDRESS,
    EVM_PRIVATE_KEY,
    KAKAROT_CHAIN_ID,
    NETWORK,
)
from scripts.utils.starknet import call as _call_starknet
from scripts.utils.starknet import fund_address as _fund_starknet_address
from scripts.utils.starknet import get_contract as _get_starknet_contract
from scripts.utils.starknet import get_deployments
from scripts.utils.starknet import invoke as _invoke_starknet
from scripts.utils.starknet import wait_for_transaction

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not NETWORK["devnet"]:
    try:
        fetch_deployments()
    except Exception as e:
        logger.warn(f"Using network {NETWORK}, couldn't fetch deployment, error:\n{e}")

try:
    FOUNDRY_FILE = toml.loads((Path(__file__).parents[2] / "foundry.toml").read_text())
except (NameError, FileNotFoundError):
    FOUNDRY_FILE = toml.loads(Path("foundry.toml").read_text())


class EvmTransactionError(Exception):
    pass


@functools.lru_cache()
def get_contract(
    contract_app: str,
    contract_name: str,
    address=None,
    caller_eoa: Optional[Account] = None,
) -> Web3Contract:
    all_compilation_outputs = [
        json.load(open(file))
        for file in Path(FOUNDRY_FILE["profile"]["default"]["out"]).glob(
            f"**/{contract_name}.json"
        )
    ]
    if len(all_compilation_outputs) == 1:
        target_compilation_output = all_compilation_outputs[0]
    else:
        target_solidity_file_path = list(
            (Path(FOUNDRY_FILE["profile"]["default"]["src"]) / contract_app).glob(
                f"**/{contract_name}.sol"
            )
        )
        if len(target_solidity_file_path) != 1:
            raise ValueError(
                f"Cannot locate a unique {contract_name} in {contract_app}"
            )

        target_compilation_output = [
            compilation
            for compilation in all_compilation_outputs
            if compilation["metadata"]["settings"]["compilationTarget"].get(
                str(target_solidity_file_path[0])
            )
        ]

        if len(target_compilation_output) != 1:
            raise ValueError(
                f"Cannot locate a unique compilation output for target {target_solidity_file_path[0]}: "
                f"found {len(target_compilation_output)} outputs:\n{target_compilation_output}"
            )
        target_compilation_output = target_compilation_output[0]

    contract = cast(
        Web3Contract,
        Web3().eth.contract(
            address=to_checksum_address(address) if address is not None else address,
            abi=target_compilation_output["abi"],
            bytecode=target_compilation_output["bytecode"]["object"],
        ),
    )

    try:
        for fun in contract.functions:
            setattr(contract, fun, MethodType(_wrap_kakarot(fun, caller_eoa), contract))
    except NoABIFunctionsFound:
        pass
    contract.events.parse_starknet_events = MethodType(_parse_events, contract.events)
    return contract


async def deploy(
    contract_app: str, contract_name: str, *args, **kwargs
) -> Web3Contract:
    logger.info(f"⏳ Deploying {contract_name}")
    caller_eoa = kwargs.pop("caller_eoa", None)
    contract = get_contract(contract_app, contract_name, caller_eoa=caller_eoa)
    max_fee = kwargs.pop("max_fee", None)
    value = kwargs.pop("value", 0)
    receipt, response, success = await eth_send_transaction(
        to=0,
        gas=int(1e18),
        data=contract.constructor(*args, **kwargs).data_in_transaction,
        caller_eoa=caller_eoa,
        max_fee=max_fee,
        value=value,
    )
    if success == 0:
        raise EvmTransactionError(response)

    starknet_address, evm_address = response
    contract.address = Web3.to_checksum_address(f"0x{evm_address:040x}")
    contract.starknet_address = starknet_address
    logger.info(f"✅ {contract_name} deployed at address {contract.address}")

    return contract


def _parse_events(cls: ContractEvents, starknet_events: List[Event]):
    kakarot_address = get_deployments()["kakarot"]["address"]
    kakarot_events = [
        event
        for event in starknet_events
        if event.from_address == kakarot_address and event.keys[0] < 2**160
    ]
    log_receipts = [
        LogReceipt(
            address=to_checksum_address(f"0x{event.keys[0]:040x}"),
            blockHash=bytes(),
            blockNumber=bytes(),
            data=bytes(event.data),
            logIndex=log_index,
            topic=bytes(),
            topics=[
                bytes.fromhex(
                    # event "keys" in cairo are event "topics" in solidity
                    # they're returned as list where consecutive values are indeed
                    # low, high, low, high, etc. of the Uint256 cairo representation
                    # of the bytes32 topics. This recomputes the original topic
                    f"{(event.keys[i] + 2**128 * event.keys[i + 1]):064x}"
                )
                # every kkrt evm event emission appends the emitting contract as the first value of the event key (as felt), we skip those here
                for i in range(1, len(event.keys), 2)
            ],
            transactionHash=bytes(),
            transactionIndex=0,
        )
        for log_index, event in enumerate(kakarot_events)
    ]

    return {
        event_abi.get("name"): _get_matching_logs_for_event(event_abi, log_receipts)
        for event_abi in cls._events
    }


def _get_matching_logs_for_event(event_abi, log_receipts) -> List[dict]:
    logs = []
    codec = Web3().codec
    for log_receipt in log_receipts:
        try:
            event_data = get_event_data(codec, event_abi, log_receipt)
            logs += [event_data["args"]]
        except (MismatchedABI, LogTopicError, InsufficientDataBytes):
            pass
    return logs


def _wrap_kakarot(fun: str, caller_eoa: Optional[Account] = None):
    """Wrap a contract function call with the Kakarot contract."""

    async def _wrapper(self, *args, **kwargs):
        abi = self.get_function_by_name(fun).abi
        gas_price = kwargs.pop("gas_price", 1_000)
        gas_limit = kwargs.pop("gas_limit", 1_000_000_000)
        value = kwargs.pop("value", 0)
        caller_eoa_ = kwargs.pop("caller_eoa", caller_eoa)
        max_fee = kwargs.pop("max_fee", None)
        calldata = self.get_function_by_name(fun)(
            *args, **kwargs
        )._encode_transaction_data()

        if abi["stateMutability"] in ["pure", "view"]:
            kakarot_contract = await _get_starknet_contract("kakarot")
            origin = (
                int(caller_eoa_.signer.public_key.to_address(), 16)
                if caller_eoa_
                else int(EVM_ADDRESS, 16)
            )
            result = await kakarot_contract.functions["eth_call"].call(
                origin=origin,
                to=int(self.address, 16),
                gas_limit=gas_limit,
                gas_price=gas_price,
                value=value,
                data=list(HexBytes(calldata)),
            )
            if result.success == 0:
                raise EvmTransactionError(result.return_data)
            codec = Web3().codec
            types = [o["type"] for o in abi["outputs"]]
            decoded = codec.decode(types, bytes(result.return_data))
            normalized = map_abi_data(BASE_RETURN_NORMALIZERS, types, decoded)
            return normalized[0] if len(normalized) == 1 else normalized

        logger.info(f"⏳ Executing {fun} at address {self.address}")
        receipt, response, success = await eth_send_transaction(
            to=self.address,
            value=value,
            gas=gas_limit,
            data=calldata,
            caller_eoa=caller_eoa_ if caller_eoa_ else None,
            max_fee=max_fee,
        )
        if success == 0:
            logger.error(f"❌ {self.address}.{fun} failed")
            raise EvmTransactionError(response)
        logger.info(f"✅ {self.address}.{fun}")
        return receipt

    return _wrapper


async def _contract_exists(address: int) -> bool:
    try:
        await CLIENT.get_class_hash_at(address)
        return True
    except ClientError:
        return False


async def get_eoa(private_key=None, amount=10) -> Account:
    private_key = private_key or keys.PrivateKey(bytes.fromhex(EVM_PRIVATE_KEY[2:]))
    starknet_address = await deploy_and_fund_evm_address(
        private_key.public_key.to_checksum_address(), amount
    )

    return Account(
        address=starknet_address,
        client=CLIENT,
        chain=NETWORK["chain_id"],
        # This is somehow a hack because we put EVM private key into a
        # Stark signer KeyPair to have both a regular Starknet account
        # and the access to the private key
        key_pair=KeyPair(int(private_key), private_key.public_key),
    )


async def eth_send_transaction(
    to: Union[int, str],
    data: Union[str, bytes],
    gas: int = 21_000,
    value: Union[int, str] = 0,
    caller_eoa: Optional[Account] = None,
    max_fee: Optional[int] = None,
):
    """Execute the data at the EVM contract to on Kakarot."""
    evm_account = caller_eoa or await get_eoa()

    typed_transaction = TypedTransaction.from_dict(
        {
            "type": 0x2,
            "chainId": KAKAROT_CHAIN_ID,
            "nonce": await evm_account.get_nonce(),
            "gas": gas,
            "maxPriorityFeePerGas": int(1e19),
            "maxFeePerGas": int(1e19),
            "to": to_checksum_address(to) if to else None,
            "value": value,
            "data": data,
        }
    )
    evm_tx = EvmAccount.sign_transaction(
        typed_transaction.as_dict(),
        hex(evm_account.signer.private_key),
    )
    response = await evm_account.execute(
        calls=Call(
            to_addr=0xDEAD,  # unused in current EOA implementation
            selector=0xDEAD,  # unused in current EOA implementation
            calldata=evm_tx.rawTransaction,
        ),
        max_fee=int(5e17) if max_fee is None else max_fee,
    )
    await wait_for_transaction(tx_hash=response.transaction_hash)
    receipt = await CLIENT.get_transaction_receipt(response.transaction_hash)
    transaction_events = [
        event
        for event in receipt.events
        if event.from_address == evm_account.address
        and event.keys[0] == starknet_keccak(b"transaction_executed")
    ]
    if len(transaction_events) != 1:
        raise ValueError("Cannot locate the single event giving the actual tx status")
    (
        msg_hash_low,
        msg_hash_high,
        response_len,
        *response,
        success,
    ) = transaction_events[0].data

    if response_len != len(response):
        raise ValueError("Not able to parse event data")

    return receipt, response, success


async def compute_starknet_address(address: Union[str, int]):
    evm_address = int(address, 16) if isinstance(address, str) else address
    kakarot_contract = await _get_starknet_contract("kakarot")
    return (
        await kakarot_contract.functions["compute_starknet_address"].call(evm_address)
    ).contract_address


async def deploy_and_fund_evm_address(evm_address: str, amount: float):
    """
    Deploy an EOA linked to the given EVM address and fund it with amount ETH.
    """
    starknet_address = (
        await _call_starknet(
            "kakarot", "compute_starknet_address", int(evm_address, 16)
        )
    ).contract_address

    if not await _contract_exists(starknet_address):
        await fund_address(evm_address, amount)
        await _invoke_starknet(
            "kakarot", "deploy_externally_owned_account", int(evm_address, 16)
        )
    return starknet_address


async def fund_address(address: Union[str, int], amount: float):
    starknet_address = await compute_starknet_address(address)
    logger.info(
        f"ℹ️  Funding EVM address {address} at Starknet address {hex(starknet_address)}"
    )
    await _fund_starknet_address(starknet_address, amount)


async def store_bytecode(bytecode: Union[str, bytes], **kwargs):
    """
    Deploy a contract account through Kakarot with given bytecode as finally
    stored bytecode.

    Note: Deploying directly a contract account and using `write_bytecode` would not
    produce an EVM contract registered in Kakarot and thus is not an option. We need
    to have Kakarot deploying EVM contracts.
    """
    bytecode = (
        bytecode
        if isinstance(bytecode, bytes)
        else bytes.fromhex(bytecode.replace("0x", ""))
    )

    # Defines variables for used opcodes to make it easier to write the mnemonic
    PUSH1 = "60"
    PUSH2 = "61"
    CODECOPY = "39"
    RETURN = "f3"
    # The deploy_bytecode is crafted such that:
    # - append at the end of the run bytecode the target bytecode
    # - load this chunk of code in memory using CODECOPY
    # - return this data in RETURN
    #
    # Bytecode usage
    # - CODECOPY(len, offset, destOffset): set memory such that memory[destOffset:destOffset + len] = code[offset:offset + len]
    # - RETURN(len, offset): return memory[offset:offset + len]
    deploy_bytecode = bytes.fromhex(
        f"""
    {PUSH2} {len(bytecode):04x}
    {PUSH1} 0e
    {PUSH1} 00
    {CODECOPY}
    {PUSH2} {len(bytecode):04x}
    {PUSH1} 00
    {RETURN}
    {bytecode.hex()}"""
    )
    receipt, response, success = await eth_send_transaction(
        to=0, data=deploy_bytecode, **kwargs
    )
    assert success
    starknet_address, evm_address = response
    stored_bytecode = await get_bytecode(evm_address)
    assert stored_bytecode == bytecode
    return evm_address


async def get_bytecode(address: Union[int, str]):
    starknet_address = await compute_starknet_address(address)
    return bytes(
        (
            await _call_starknet(
                "contract_account", "bytecode", address=starknet_address
            )
        ).bytecode
    )
