# Copyright 2023 Hunki Enterprises BV
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import traceback
import pytz

from odoo import http, _
from werkzeug.exceptions import BadRequest
from werkzeug.utils import dump_cookie
from datetime import datetime


class EdiPunchoutController(http.Controller):
    @http.route(
        "/edi_punchout/get_transaction", type="http", auth="user", methods=["get"],
    )
    def get_transaction(self, account_id, order_id=None):
        transaction = (
            http.request.env["edi.punchout.transaction"]
            .sudo()
            .create(
                {
                    "session_id": http.request.session.sid,
                    "account_id": account_id,
                    "order_id": order_id,
                }
            )
        )
        response = http.request.make_response(transaction.transaction_id)
        response.headers.add('Set-Cookie', dump_cookie(
            "edi_punchout_client_key_%s" % transaction.id,
            transaction.client_key,
            expires=self._current_timestamp()+3600,
            httponly=True,
            secure=True,
        ) + "; SameSite=None")
        # werkzeug 0.14 does not support setting SameSite to 'None'
        # https://github.com/pallets/werkzeug/issues/1549
        return response

    @http.route(
        "/edi_punchout/return/<string:transaction_id>",
        type="http",
        methods=["post"],
        csrf=False,
        auth="public",
    )
    def return_hook(self, transaction_id, *args, **kwargs):
        transaction = (
            http.request.env["edi.punchout.transaction"]
            .sudo()
            .search([("transaction_id", "=", transaction_id)])
        )
        if (
            transaction
            and http.request.httprequest.cookies.get(
                "edi_punchout_client_key_%s" % transaction.id
            )
            == transaction.client_key
        ):
            http.request.uid = transaction.create_uid.id
            http.request.env.company = transaction.account_id.company_id
            transaction = transaction.with_user(transaction.create_uid).with_context(allowed_company_ids=http.request.env.company.ids)
            transaction.request = json.dumps(http.request.httprequest.form)
            account = transaction.account_id
            account.check_access_rights("read")
            account.check_access_rule("read")
            order = transaction.with_user(http.request.env.user).order_id
            try:
                return getattr(
                    self, "_%s_return_hook" % transaction.account_id.protocol
                )(account, order)
            except Exception:
                http.request.env.cr.rollback()
                transaction.write(
                    {
                        "exception": "".join(traceback.format_exc()),
                        "request": json.dumps(http.request.httprequest.form),
                    }
                )
                http.request.env.cr.commit()
                raise
        else:
            return http.request.not_found()

    def _redirect_order(self, order):
        return http.redirect_with_hash(
            "/web#id=%d&model=purchase.order&view_type=form&action=%d"
            % (order.id, http.request.env.ref("purchase.purchase_rfq").id,)
        )

    def _oci_return_hook(self, account, order=None):
        form = http.request.httprequest.form
        # parse nonstandard list form values (VAL[1]=val1&VAL[2]=val2) into list of dicts
        # {VAL: val1}, {VAL: val2}
        prefix = "NEW_ITEM-"
        product_keys = {
            key[len(prefix) : key.index("[")]
            for key in form
            if key.startswith(prefix) and key.endswith("]")
        }
        product_dicts = []
        index = 1
        while any("%s%s[%d]" % (prefix, key, index) in form for key in product_keys):
            product_dict = {
                key: form["%s%s[%d]" % (prefix, key, index)]
                for key in product_keys
                if "%s%s[%d]" % (prefix, key, index) in form
            }
            if "NEW_ITEM-LONGTEXT_%d:132[]" % index in form:
                product_dict["LONGTEXT"] = form["NEW_ITEM-LONGTEXT_%d:132[]" % index]
            product_dicts.append(product_dict)
            index += 1
        order = account._handle_return(product_dicts)
        return self._redirect_order(order)

    def _ids_return_hook(self, account, order=None):
        shopping_cart = http.request.httprequest.form.get("warenkorb",
            http.request.httprequest.form.get("Warenkorb")
        )
        if shopping_cart is None:
            raise BadRequest(_("The request form is missing the shopping cart key!"))

        order = account._handle_return(shopping_cart, order)
        return self._redirect_order(order)

    @staticmethod
    def _current_timestamp():
        user_tz = pytz.timezone(http.request.env.user.tz if http.request.env.user.tz else "utc")
        return (
            (pytz.utc.localize(datetime.now()))
            .astimezone(user_tz)
            .replace(tzinfo=None)
            .timestamp()
        )
