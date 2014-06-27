# -*- coding: utf-8 -*-

from tables import (ReceivedTransactionsTable, SentTransactions,
    TransactionsToPduTable, PdusTable, StatePdusTable, PduEdgesTable,
    PduForwardExtremetiesTable, PduBackwardExtremetiesTable, CurrentStateTable)
from .. import utils

from twisted.internet import defer

from collections import namedtuple

import logging

#dbpool = None  # XXX: We need to do something.

logger = logging.getLogger("synapse.persistence.transactions")

PduTuple = namedtuple("PduTuple", ("pdu_entry", "state_entry", "prev_pdu_list"))
TransactionTuple = namedtuple("TransactionTuple", ("tx_entry", "prev_ids"))
PduIdTuple = namedtuple("PduIdTuple", ("pdu_id", "origin"))


class TransactionQueries(object):

    @classmethod
    @defer.inlineCallbacks
    def get_response_for_received(clz, transaction_id, origin):
        result = yield utils.get_db_pool().runInteraction(
            clz._get_received_interaction,
            transaction_id, origin)

        if result and result.response_code:
            defer.returnValue(
                    (result.response_code, result.response_json)
                )
        else:
            defer.returnValue(None)

    @classmethod
    def insert_received(clz, tx_tuple):
        #entry = ReceivedTransactionsTable.EntryType(**pdu_tuple.entry)
        return utils.DBPOOL.runInteraction(clz._insert_interaction, tx_tuple)

    @classmethod
    def set_recieved_txn_response(clz, transaction_id, origin, code,
    response_json):
        return utils.get_db_pool().runInteraction(
            clz._set_recieved_response_interaction,
            transaction_id, origin, code, response_json)

    @classmethod
    def prep_send_transaction(clz, transaction_id, destination, ts,
    pdu_list):
        return utils.get_db_pool().runInteraction(
            clz._prep_send_transaction_interaction,
            transaction_id, destination, ts, pdu_list)

    @classmethod
    def delivered_txn(clz, transaction_id, destination,
    code, response_json):
        return utils.get_db_pool().runInteraction(
            clz._delivered_txn_interaction,
            transaction_id, destination, code, response_json)

    @classmethod
    def get_transactions_after(clz, transaction_id, destination):
        return utils.get_db_pool().runInteraction(
            clz._get_transactions_after_interaction,
            transaction_id, destination)

    @staticmethod
    def _get_sent_interaction(txn, transaction_id, destination):
        where_clause = "transaction_id = ? AND destination = ?"
        query = SentTransactions.select_statement(where_clause)

        txn.execute(query, (transaction_id,))

        results = SentTransactions.decode_results(txn.fetchall())

        if len(results) == 0:
            return None
        else:
            return results[1]

    @staticmethod
    def _get_received_interaction(txn, transaction_id, origin):
        where_clause = "transaction_id = ? AND origin = ?"
        query = ReceivedTransactionsTable.select_statement(where_clause)

        txn.execute(query, (transaction_id, origin))

        results = ReceivedTransactionsTable.decode_results(txn.fetchall())

        if len(results) == 0:
            return None
        else:
            return results[1]

    @staticmethod
    def _insert_received_interaction(txn, tx_tuple):
        query = ReceivedTransactionsTable.insert_statement()

        txn.execute(query, tx_tuple.tx_entry)

        query = (
                "UPDATE %s SET has_been_referenced = 1 "
                "WHERE transaction_id = ? AND orign = ?"
                ) % ReceivedTransactionsTable.table_name

        origin = tx_tuple.tx_entry.origin

        txn.executemany(query, [
                (tx_id, origin) for tx_id in tx_tuple.prev_ids
            ])

    @staticmethod
    def _set_recieved_response_interaction(txn, transaction_id, origin,
    code, response_json):
        query = ("UPDATE %s "
            "SET response_code = ?, response_json = ? "
            "WHERE transaction_id = ? AND origin = ?"
            ) % ReceivedTransactionsTable.table_name

        txn.execute(query, (code, response_json, transaction_id, origin))

    @staticmethod
    def _delivered_txn_interaction(txn, transaction_id, destination,
    code, response_json):
        # Say what response we got.
        query = ("UPDATE %s "
            "SET response_code = ?, response_json = ? "
            "WHERE transaction_id = ? AND destination = ?"
            ) % SentTransactions.table_name

        txn.execute(query, (code, response_json, transaction_id, destination))

    @staticmethod
    def _prep_send_transaction_interaction(txn, transaction_id, destination,
    ts, pdu_list):
        # First we find out what the prev_txs should be.
        # Since we know that we are only sending one transaction at a time,
        # we can simply take the last one.
        query = "%s ORDER BY id DESC LIMIT 1" % (
                SentTransactions.select_statement("destination = ?"),
            )

        results = txn.execute(query, (destination,))
        results = SentTransactions.decode_results(results)

        prev_txns = [r.transaction_id for r in results]

        # Actually add the new transaction to the sent_transactions table.

        query = SentTransactions.insert_statement()
        txn.execute(query, SentTransactions.EntryType(
                None,
                transaction_id=transaction_id,
                destination=destination,
                ts=ts,
                response_code=0,
                response_json=None
            ))

        # Update the tx id -> pdu id mapping

        values = [
                (transaction_id, destination, pdu[0], pdu[1])
                for pdu in pdu_list
            ]

        logger.debug("Inserting: %s", repr(values))

        query = TransactionsToPduTable.insert_statement()
        txn.executemany(query, values)

        return prev_txns

    @classmethod
    def _get_transactions_after_interaction(clz, txn, transaction_id,
    destination):
        where = ("destination = ? AND id > (select id FROM %s WHERE "
            "transaction_id = ? AND destination = ?)") % (
                SentTransactions.table_name
            )
        query = SentTransactions.select_statement(where)

        txn.execute(query, (destination, transaction_id, destination))

        return ReceivedTransactionsTable.decode_results(txn.fetchall())


class PduQueries(object):

    @classmethod
    def get_pdu(clz, pdu_id, origin):
        return utils.get_db_pool().runInteraction(clz._get_pdu_interaction,
             pdu_id, origin)

    @classmethod
    def get_current_state(clz, context):
        return utils.get_db_pool().runInteraction(
            clz._get_current_state_interaction,
            context)

    @classmethod
    def insert(clz, prev_pdus, **cols):
        entry = PdusTable.EntryType(
                **{k: cols.get(k, None) for k in PdusTable.fields}
            )
        return utils.get_db_pool().runInteraction(
            clz._insert_interaction,
            entry, prev_pdus)

    @classmethod
    def insert_state(clz, prev_pdus, **cols):
        pdu_entry = PdusTable.EntryType(
                **{k: cols.get(k, None) for k in PdusTable.fields}
            )
        state_entry = StatePdusTable.EntryType(
                **{k: cols.get(k, None) for k in StatePdusTable.fields}
            )

        logger.debug("Inserting pdu: %s", repr(pdu_entry))
        logger.debug("Inserting state: %s", repr(state_entry))

        return utils.get_db_pool().runInteraction(
            clz._insert_state_interaction,
            pdu_entry, state_entry, prev_pdus)

    @classmethod
    def mark_as_processed(clz, pdu_id, pdu_origin):
        return utils.get_db_pool().runInteraction(
            clz._mark_as_processed_interaction,
            pdu_id, pdu_origin
            )

    @classmethod
    def get_prev_pdus(clz, context):
        return utils.get_db_pool().runInteraction(
            clz._get_prev_pdus_interaction,
            context)

    @classmethod
    def get_after_transaction(clz, transaction_id, destination, local_server):
        return utils.get_db_pool().runInteraction(
            clz._get_pdus_after_transaction,
            transaction_id, destination, local_server)

    @classmethod
    def paginate(clz, context, version_list, limit):
        return utils.get_db_pool().runInteraction(
            clz._paginate_interaction,
            context, version_list, limit)

    @classmethod
    def is_new(clz, pdu_id, origin, context, version):
        return utils.get_db_pool().runInteraction(
            clz._is_new_interaction,
            pdu_id, origin, context, version)

    @classmethod
    def _get_pdu_interaction(clz, txn, pdu_id, origin):
        query = PdusTable.select_statement("pdu_id = ? AND origin = ?")
        txn.execute(query, (pdu_id, origin))

        results = PdusTable.decode_results(txn.fetchall())

        logger.debug("_get_pdu_interaction: pdu_id=%s, origin=%s results: %s",
            pdu_id, origin, repr(results))

        if len(results) == 1:
            pdu_entry = results[0]
        else:
            return None

        return clz._get_pdu_tuple_from_entry(txn, pdu_entry)

    @classmethod
    def _get_current_state_interaction(clz, txn, context):
        pdus_fields = ", ".join(["pdus.%s" % f for f in PdusTable.fields])

        query = ("SELECT %s FROM pdus INNER JOIN state_pdus ON "
                "state_pdus.pdu_id = pdus.pdu_id AND "
                "state_pdus.origin = pdus.origin "
                "WHERE state_pdus.context = ?") % pdus_fields

        txn.execute(query, (context,))

        pdus = PdusTable.decode_results(txn.fetchall())
        return clz._get_pdu_tuple_from_entries(txn, pdus)

    @classmethod
    def _insert_interaction(clz, txn, entry, prev_pdus):
        txn.execute(PdusTable.insert_statement(), entry)

        clz._handle_prev_pdus(txn, entry.outlier, entry.pdu_id, entry.origin,
            prev_pdus, entry.context)

    @classmethod
    def _insert_state_interaction(clz, txn, pdu_entry,
    state_entry, prev_pdus):
        txn.execute(PdusTable.insert_statement(), pdu_entry)
        txn.execute(StatePdusTable.insert_statement(), state_entry)

        clz._handle_prev_pdus(txn,
             pdu_entry.outlier, pdu_entry.pdu_id, pdu_entry.origin, prev_pdus,
             pdu_entry.context)

    @staticmethod
    def _mark_as_processed_interaction(txn, pdu_id, pdu_origin):
        txn.execute("UPDATE %s SET have_processed = 1" % PdusTable.table_name)

    @staticmethod
    def _handle_prev_pdus(txn, outlier, pdu_id, origin, prev_pdus,
    context):
        txn.executemany(PduEdgesTable.insert_statement(),
                [(pdu_id, origin, p[0], p[1], context) for p in prev_pdus]
            )

        ## Update the extremeties table if this is not an outlier.
        if not outlier:

            # First, we delete any from the forwards extremeties table.
            query = ("DELETE FROM %s WHERE pdu_id = ? AND origin = ?" %
                PduForwardExtremetiesTable.table_name)
            txn.executemany(query, prev_pdus)

            # Also delete from the backwards extremeties table all ones that
            # reference pdus that we have already seen
            query = (
                "DELETE FROM %(pdu_back)s WHERE EXISTS ("
                    "SELECT 1 FROM %(pdus)s AS pdus "
                    "WHERE "
                    "%(pdu_back)s.pdu_id = pdus.pdu_id "
                    "AND %(pdu_back)s.origin = pdus.origin"
                ")"
                ) % {
                    "pdu_back": PduBackwardExtremetiesTable.table_name,
                    "pdus": PdusTable.table_name,
                }
            txn.execute(query)

            # We only insert as a forward extremety the new pdu if there are no
            # other pdus that reference it as a prev pdu
            query = (
                "INSERT INTO %(table)s (pdu_id, origin, context) "
                "SELECT ?, ?, ? WHERE NOT EXISTS ("
                    "SELECT 1 FROM %(pdu_edges)s WHERE "
                    "prev_pdu_id = ? AND prev_origin = ?"
                ")"
                ) % {
                    "table": PduForwardExtremetiesTable.table_name,
                    "pdu_edges": PduEdgesTable.table_name
                }

            logger.debug("query: %s", query)

            txn.execute(query, (pdu_id, origin, context, pdu_id, origin))

            txn.executemany(PduBackwardExtremetiesTable.insert_statement(),
                [(i, o, context) for i, o in prev_pdus]
            )

    @staticmethod
    def _get_prev_pdus_interaction(txn, context):
        query = (
                "SELECT p.pdu_id, p.origin, p.version FROM %(pdus)s as p "
                "INNER JOIN %(forward)s as f ON p.pdu_id = f.pdu_id "
                "AND f.origin = p.origin "
                "WHERE f.context = ?"
                ) % {
                        "pdus": PdusTable.table_name,
                        "forward": PduForwardExtremetiesTable.table_name,
                    }

        logger.debug("get_prev query: %s" % query)

        txn.execute(
            query,
            (context, )
        )

        results = txn.fetchall()

        return [(row[0], row[1], row[2]) for row in results]

    @classmethod
    def _get_pdus_after_transaction(clz, txn, transaction_id, destination,
    local_server):
        pdus_fields = ", ".join(["pdus.%s" % f for f in PdusTable.fields])

        # Query that first get's all transaction_ids with an id greater than
        # the one given from the `sent_transactions` table. Then JOIN on this
        # from the `tx->pdu` table to get a list of (pdu_id, origin) that
        # specify the pdus that were sent in those transactions.
        # Finally, JOIN that from the `pdus` table to get all the attributes of
        # those PDUs.
        query = (
            "SELECT %(fields)s FROM pdus INNER JOIN ("
                "SELECT pdu_id, pdu_origin FROM %(tx_pdu)s as tp "
                "INNER JOIN %(sent_tx)s as st "
                    "ON tp.transaction_id = st.transaction_id "
                    "AND tp.destination = st.destination "
                "WHERE st.id > ("
                    "SELECT id FROM %(sent_tx)s "
                    "WHERE transaction_id = ? AND destination = ?"
                ")"
            ") AS t ON t.pdu_id = pdus.pdu_id AND pdus.origin = t.pdu_origin;"
        ) % {
            "fields": pdus_fields,
            "tx_pdu": TransactionsToPduTable.table_name,
            "sent_tx": SentTransactions.table_name,
        }

        txn.execute(query, (transaction_id, destination))

        pdus = PdusTable.decode_results(txn.fetchall())

        return clz._get_pdu_tuple_from_entries(txn, pdus)

    @classmethod
    def _get_pdu_tuple_from_entries(clz, txn, pdu_entries):
        return [clz._get_pdu_tuple_from_entry(txn, pdu) for pdu in pdu_entries]

    @staticmethod
    def _get_pdu_tuple_from_entry(txn, pdu_entry):
        txn.execute(
            PduEdgesTable.select_statement("pdu_id = ? AND origin = ?"),
            (pdu_entry.pdu_id, pdu_entry.origin)
        )

        edges = [(r.prev_pdu_id, r.prev_origin)
            for r in PduEdgesTable.decode_results(txn.fetchall())]

        txn.execute(
            StatePdusTable.select_statement("pdu_id = ? AND origin = ?"),
            (pdu_entry.pdu_id, pdu_entry.origin)
        )

        states = StatePdusTable.decode_results(txn.fetchall())

        if states:
            state_entry = states[0]
        else:
            state_entry = None

        return PduTuple(pdu_entry, state_entry, edges)

    @classmethod
    def _paginate_interaction(clz, txn, context, version_list, limit):
        logger.debug("_paginate_interaction: %s, %s, %s",
            context, repr(version_list), limit)

        pdu_fields = ", ".join(["p.%s" % f for f in PdusTable.fields])

        pdu_results = []

        front = version_list

        new_front = [1]  # Initially seed with a value so that we always do
                         # at lest one loop

        # We iterate through all pdu_ids in `front` to select their previous
        # pdus. These are dumped in `new_front`. We continue until we reach the
        # limit *or* new_front is empty (i.e., we've run out of things to
        # select
        while new_front and len(pdu_results) < limit:

            new_front = []
            for pdu_id, origin in front:
                logger.debug("_paginate_interaction: i=%s, o=%s",
                    pdu_id, origin)
                query = (
                    "SELECT %(pdu_fields)s from %(pdu_table)s as p "
                    "INNER JOIN %(edges_table)s as e ON "
                        "p.pdu_id = e.prev_pdu_id "
                        "AND p.origin = e.prev_origin "
                        "AND p.context = e.context "
                    "WHERE e.context = ? AND e.pdu_id = ? AND e.origin = ? "
                    "LIMIT ?"
                    %
                        {
                            "pdu_fields": pdu_fields,
                            "pdu_table": PdusTable.table_name,
                            "edges_table": PduEdgesTable.table_name,
                        }
                    )

                txn.execute(
                    query,
                    (context, pdu_id, origin, limit - len(pdu_results))
                )

                entries = PdusTable.decode_results(txn.fetchall())

                for row in entries:
                    logger.debug("_paginate_interaction: got i=%s, o=%s",
                        row.pdu_id, row.origin)
                    new_front.append((row.pdu_id, row.origin))
                    pdu_results.append(row)

            front = new_front

        # We also want to update the `prev_pdus` attributes before returning.
        return clz._get_pdu_tuple_from_entries(txn, pdu_results)

    @staticmethod
    def _is_new_interaction(txn, pdu_id, origin, context, version):
        ## If version > min version in back table, then we classify it as new.
        ## OR if there is nothing in the back table, then it kinda needs to
        ## be a new thing.
        query = (
            "SELECT min(p.version) FROM %(edges)s as e "
            "INNER JOIN %(back)s as b "
                "ON e.prev_pdu_id = b.pdu_id AND e.prev_origin = b.origin "
            "INNER JOIN %(pdus)s as p "
                "ON e.pdu_id = p.pdu_id AND p.origin = e.origin "
            "WHERE p.context = ?"
            ) % {
                    "pdus": PdusTable.table_name,
                    "edges": PduEdgesTable.table_name,
                    "back": PduBackwardExtremetiesTable.table_name,
                }

        txn.execute(query, (context,))

        min_version, = txn.fetchone()

        if not min_version or version > int(min_version):
            logger.debug("is_new true: id=%s, o=%s, v=%s min_ver=%s",
                pdu_id, origin, version, min_version)
            return True

        ## If this pdu is in the forwards table, then it also is a nwe one
        query = (
            "SELECT * FROM %(forward)s WHERE pdu_id = ? AND origin = ?"
            ) % {
                    "forward": PduForwardExtremetiesTable.table_name,
                }

        txn.execute(query, (pdu_id, origin))

        # Did we get anything?
        if txn.fetchall():
            logger.debug("is_new true: id=%s, o=%s, v=%s was forward",
                pdu_id, origin, version)
            return True

        logger.debug("is_new false: id=%s, o=%s, v=%s",
                pdu_id, origin, version)

        ## FINE THEN. It's probably old.
        return False


class StateQueries(object):
    @classmethod
    def get_next_missing_pdu(clz, new_pdu):
        return utils.get_db_pool().runInteraction(
            clz._get_next_missing_pdu_interaction,
            new_pdu)

    @classmethod
    def handle_new_state(clz, new_pdu):
        return utils.get_db_pool().runInteraction(
            clz._handle_new_state_interaction,
            new_pdu)

    @classmethod
    def _get_next_missing_pdu_interaction(clz, txn, new_pdu):
        logger.debug("_get_next_missing_pdu_interaction %s %s",
            new_pdu.pdu_id, new_pdu.origin)

        current = clz._get_current_interaction(txn, new_pdu)

        if not current:
            return None

        # Oh look, it's a straight clobber, so wooooo almost no-op.
        if (new_pdu.prev_state_id == current.pdu_id
        and new_pdu.prev_state_origin == current.origin):
            return None

        ##
        # Ok, now loop through until we get to a common ancestor.
        branch_a = new_pdu
        branch_b = current

        get_query = (
            "SELECT %(fields)s FROM %(state)s "
            "WHERE pdu_id = ? AND origin = ? "
            ) % {
                    "fields": StatePdusTable.get_fields_string(),
                    "state": StatePdusTable.table_name,
                }

        while True:
            if (branch_a.pdu_id == branch_b.pdu_id
                    and branch_a.origin == branch_b.origin):
                break

            if branch_a.version < branch_b:
                branch = branch_a
            else:
                branch = branch_b

            txn.execute(get_query, (branch.pdu_id, branch.origin))

            results = StatePdusTable.decode_results(txn.fetchall())

            if not results:
                return PduIdTuple(branch.pdu_id, branch.origin)

        return None

    @classmethod
    def _handle_new_state_interaction(clz, txn, new_pdu):
        logger.debug("_handle_new_state_interaction %s %s",
            new_pdu.pdu_id, new_pdu.origin)

        current = clz._get_current_interaction(txn, new_pdu)

        is_current = False

        if not current:
            # Oh, we don't have any state for this yet.
            is_current = True
        elif (current.pdu_id == new_pdu.prev_state_id
                and current.origin == new_pdu.prev_state_origin):
            # Oh! A direct clobber. Just do it.
            is_current = True
        else:
            # We need to iterate through the tree.
            branch_new = new_pdu
            branch_current = current

            max_new = int(new_pdu.power_level)
            max_current = int(current.power_level)

            while True:
                if (branch_new.pdu_id == branch_current.pdu_id
                        and branch_new.origin == branch_current.origin):
                    # Woo! We found a common ancestor, is the new_pdu actually
                    # new?
                    is_current = max_new > max_current
                    break

                if int(branch_new.version) < int(branch_current.version):
                    txn.execute(get_query,
                        (branch_new.pdu_id, branch_new.origin))

                    branch_new = StatePdusTable.decode_results(
                            txn.fetchall()
                        )[0]

                    max_new = max(int(branch_new.version), max_new)
                else:
                    txn.execute(get_query,
                        (branch_current.pdu_id, branch_current.origin))

                    branch_current = StatePdusTable.decode_results(
                            txn.fetchall()
                        )[0]

                    max_current = max(int(branch_current.version), max_current)

                # XXX: We should actually do some checks and then raise.
                # We currenttly assume that we have all the state_pdus
                # for example.

        if is_current:
            logger.debug("_handle_new_state_interaction make current")

            # Right, this is a new thing, so woo, just insert it.
            txn.execute(
                "INSERT INTO %(curr)s (%(fields)s) VALUES (%(qs)s)"
                % {
                    "curr": CurrentStateTable.table_name,
                    "fields": CurrentStateTable.get_fields_string(),
                    "qs": ", ".join(["?"] * len(CurrentStateTable.fields))
                },
                CurrentStateTable.EntryType(
                    *(new_pdu.__dict__[k] for k in CurrentStateTable.fields)
                )
            )
        else:
            logger.debug("_handle_new_state_interaction not current")

        logger.debug("_handle_new_state_interaction done")

        return is_current

    @staticmethod
    def _get_current_interaction(txn, pdu):
        logger.debug("_get_current_interaction %s %s",
            pdu.pdu_id, pdu.origin)

        current_query = (
            "SELECT %(fields)s FROM %(state)s as s "
            "INNER JOIN %(curr)s as c "
                "ON s.pdu_id = c.pdu_id AND s.origin = c.origin "
            "WHERE s.context = ? AND s.pdu_type = ? AND s.state_key = ? "
            ) % {
                    "fields": StatePdusTable.get_fields_string(prefix="s"),
                    "curr": CurrentStateTable.table_name,
                    "state": StatePdusTable.table_name,
                }

        txn.execute(current_query,
            (pdu.context, pdu.pdu_type, pdu.state_key))

        results = StatePdusTable.decode_results(txn.fetchall())

        if not results:
            logger.debug("_get_current_interaction not found")
            return None

        # We should only ever get 0 or 1 due to table restraints
        current = results[0]

        logger.debug("_get_current_interaction found %s %s",
            current.pdu_id, current.origin)
        return current