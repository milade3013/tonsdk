import decimal
from functools import reduce

from .. import Contract
from ._wallet_contract import WalletContract, SendModeEnum
from ...boc import Cell, begin_cell, begin_dict
from ...utils import Address, sign_message, HighloadQueryId, check_timeout, to_nano


class OPEnum:
    InternalTransfer = 0xae42e5a4
    OutActionSendMsg = 0x0ec3c86d


class HighloadWalletV3ContractBase(WalletContract):

    def create_data_cell(self):
        cell = Cell()
        cell.bits.write_bytes(self.options["public_key"])
        cell.bits.write_uint(self.options["wallet_id"], 32)
        cell.bits.write_uint(0, 1)  # empty old_queries
        cell.bits.write_uint(0, 1)  # empty queries
        cell.bits.write_uint(0, 64)  # last_clean_time
        cell.bits.write_uint(self.options["timeout"], 22)
        return cell

    def create_signing_message(
            self,
            query_id: HighloadQueryId,
            created_at: int,
            send_mode: int,
            message_to_send,
    ):
        cell = Cell()
        cell.bits.write_uint(self.options["wallet_id"], 32)
        cell.refs.append(message_to_send)
        cell.bits.write_uint(send_mode, 8)
        cell.bits.write_uint(query_id.query_id, 23)
        cell.bits.write_uint(created_at, 64)
        cell.bits.write_uint(self.options["timeout"], 22)
        return cell


class HighloadWalletV3Contract(HighloadWalletV3ContractBase):
    def __init__(self, **kwargs):
        # https://github.com/ton-blockchain/highload-wallet-contract-v3
        self.code = "b5ee9c7241021001000228000114ff00f4a413f4bcf2c80b01020120020d02014803040078d020d74bc00101c060b0915be101d0d3030171b0915be0fa4030f828c705b39130e0d31f018210ae42e5a4ba9d8040d721d74cf82a01ed55fb04e030020120050a02027306070011adce76a2686b85ffc00201200809001aabb6ed44d0810122d721d70b3f0018aa3bed44d08307d721d70b1f0201200b0c001bb9a6eed44d0810162d721d70b15800e5b8bf2eda2edfb21ab09028409b0ed44d0810120d721f404f404d33fd315d1058e1bf82325a15210b99f326df82305aa0015a112b992306dde923033e2923033e25230800df40f6fa19ed021d721d70a00955f037fdb31e09130e259800df40f6fa19cd001d721d70a00937fdb31e0915be270801f6f2d48308d718d121f900ed44d0d3ffd31ff404f404d33fd315d1f82321a15220b98e12336df82324aa00a112b9926d32de58f82301de541675f910f2a106d0d31fd4d307d30cd309d33fd315d15168baf2a2515abaf2a6f8232aa15250bcf2a304f823bbf2a35304800df40f6fa199d024d721d70a00f2649130e20e01fe5309800df40f6fa18e13d05004d718d20001f264c858cf16cf8301cf168e1030c824cf40cf8384095005a1a514cf40e2f800c94039800df41704c8cbff13cb1ff40012f40012cb3f12cb15c9ed54f80f21d0d30001f265d3020171b0925f03e0fa4001d70b01c000f2a5fa4031fa0031f401fa0031fa00318060d721d300010f0020f265d2000193d431d19130e272b1fb00b585bf03"  # noqa:E501
        kwargs["code"] = Cell.one_from_boc(self.code)
        super().__init__(**kwargs)
        if kwargs.get("wc"):
            raise ValueError("only basechain (wc = 0) supported")
        kwargs["wc"] = 0
        super().__init__(**kwargs)
        if not self.options.get("wallet_id", None):
            self.options["wallet_id"] = 0x10AD

    def create_external_message(
            self,
            signing_message: Cell,
            need_deploy: bool,
            dummy_signature=False
    ):
        signature = bytes(64) if dummy_signature else sign_message(
            bytes(signing_message.bytes_hash()), self.options['private_key']).signature

        body = Cell()
        body.bits.write_bytes(signature)
        body.refs.append(signing_message)

        state_init = None
        code = None
        data = None

        if need_deploy:
            deploy = self.create_state_init()
            state_init = deploy["state_init"]
            code = deploy["code"]
            data = deploy["data"]

        header = self.create_external_message_header(self.address)
        result_message = Contract.create_common_msg_info(
            header, state_init, body
        )

        return {
            "address": self.address,
            "message": result_message,
            "body": body,
            "signature": signature,
            "signing_message": signing_message,
            "state_init": state_init,
            "code": code,
            "data": data,
        }

    def store_order(self, order, send_mode):
        # https://github.com/ton-org/ton-core/blob/2cd5401e5607d26f151a819383a1c094bcbdbbe7/src/types/OutList.ts#L33
        # https://github.com/ton-org/ton-core/blob/2cd5401e5607d26f151a819383a1c094bcbdbbe7/src/types/OutList.ts#L49
        message_cell = begin_cell().store_cell(order).end_cell()
        return begin_cell().store_uint(OPEnum.OutActionSendMsg, 32).store_uint8(send_mode).store_ref(
            message_cell).end_cell()

    def store_orders(self, orders, send_mode):
        # https://github.com/ton-org/ton-core/blob/2cd5401e5607d26f151a819383a1c094bcbdbbe7/src/types/OutList.ts#L100
        def reducer(cell, order):
            return begin_cell().store_ref(cell).store_cell(self.store_order(order, send_mode)).end_cell()

        initial_cell = begin_cell().end_cell()
        cell = reduce(reducer, orders, initial_cell)
        return cell

    def create_internal_transfer_body(self, orders, query_id, send_mode):
        # https://github.com/ipromise2324/highload-wallet-contract-v3/blob/main/wrappers/HighloadWalletV3.ts#L123
        actions = self.store_orders(orders, send_mode)
        return begin_cell().store_uint(OPEnum.InternalTransfer, 32).store_uint(query_id.query_id, 64).store_ref(
            actions).end_cell()
        # https://github.com/ipromise2324/highload-wallet-contract-v3/blob/main/wrappers/HighloadWalletV3.ts#L167

    def create_transfer_message(
            self,
            address: str,
            amount: int,
            query_id: HighloadQueryId,
            create_at: int,
            payload: str = "",
            send_mode: int = SendModeEnum.ignore_errors | SendModeEnum.pay_gas_separately,
            need_deploy: bool = False,
            dummy_signature=False
    ):
        check_timeout(self.options["timeout"])

        if create_at is None or create_at < 0:
            raise ValueError("create_at must be number >= 0")
        message_to_send = self.create_out_msg(address, amount, payload)
        signing_message = self.create_signing_message(query_id, create_at, send_mode, message_to_send)
        return self.create_external_message(signing_message, need_deploy, dummy_signature)

    def create_multi_transfer_message(
            self,
            recipients_list: list,
            query_id: HighloadQueryId,
            create_at: int,
            send_mode: int = SendModeEnum.ignore_errors | SendModeEnum.pay_gas_separately,
            need_deploy: bool = False,
            dummy_signature=False
    ):
        check_timeout(self.options["timeout"])

        if create_at is None or create_at < 0:
            raise ValueError("create_at must be number >= 0")

        grams = 0
        orders = []
        for i, recipient in enumerate(recipients_list):
            payload = recipient.get('payload')
            payload_cell = Cell()
            if payload:
                if isinstance(payload, str):
                    if len(payload) > 0:
                        payload_cell.bits.write_uint(0, 32)
                        payload_cell.bits.write_string(payload)
                elif isinstance(payload, Cell):
                    payload_cell = payload
                else:
                    payload_cell.bits.write_bytes(payload)

            order_header = Contract.create_internal_message_header(
                recipient['address'], decimal.Decimal(recipient['amount'])
            )

            order = Contract.create_common_msg_info(order_header, None, payload_cell)
            orders.append(order)
            grams += recipient['amount']

        body = self.create_internal_transfer_body(orders, query_id, send_mode)
        order_header = self.create_internal_message_header(self.address, decimal.Decimal(grams))
        msg_to_send = Contract.create_common_msg_info(order_header, None, body)
        signing_message = self.create_signing_message(query_id, create_at, send_mode, msg_to_send)
        return self.create_external_message(signing_message, need_deploy, dummy_signature)

# https://testnet.tonviewer.com/kQBpmOmiIU0pt8nWx_VKZiOM5hvVEFmJCRZIo_q0JTY7FZS_
