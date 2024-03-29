# Electrum - lightweight Bitcoin client
# Copyright (C) 2018 The Electrum Developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import threading
import asyncio
import itertools
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Optional

from . import bitcoin
from .bitcoin import COINBASE_MATURITY, TYPE_ADDRESS, TYPE_PUBKEY
from .util import PrintError, profiler, bfh, TxMinedInfo
from .transaction import Transaction, TxOutput
from .synchronizer import Synchronizer
from .verifier import SPV
from .blockchain import hash_header
from .i18n import _

from decimal import Decimal
from .rpc import RPCHostOmni


if TYPE_CHECKING:
    from .storage import WalletStorage
    from .network import Network

TX_HEIGHT_LOCAL = -2
TX_HEIGHT_UNCONF_PARENT = -1
TX_HEIGHT_UNCONFIRMED = 0


class AddTransactionException(Exception):
    pass


class UnrelatedTransactionException(AddTransactionException):
    def __str__(self):
        return _("Transaction is unrelated to this wallet.")


class AddressSynchronizer(PrintError):
    """
    inherited by wallet
    """

    def __init__(self, storage: 'WalletStorage'):
        self.storage = storage
        self.network = None  # type: Network
        # verifier (SPV) and synchronizer are started in start_network
        self.synchronizer = None  # type: Synchronizer
        self.verifier = None  # type: SPV
        # locks: if you need to take multiple ones, acquire them in the order they are defined here!
        self.lock = threading.RLock()
        self.transaction_lock = threading.RLock()
        # address -> list(txid, height)

        # omni
        self.omni = storage.get('omni', False)
        if self.omni:
            # self.omni_balance = storage.get('omni_balance', False)

            self.omni_host = storage.get('omni_host', '')
            if self.omni_host != '':
                self.omni_daemon = RPCHostOmni()
                self.omni_daemon.set_url(self.omni_host)
            else:
                self.omni_daemon = None
            self.omni_tx = dict()

        self.history = storage.get('addr_history',{})
        # Verified transactions.  txid -> TxMinedInfo.  Access with self.lock.
        verified_tx = storage.get('verified_tx3', {})
        self.verified_tx = {}  # type: Dict[str, TxMinedInfo]
        for txid, (height, timestamp, txpos, header_hash) in verified_tx.items():
            self.verified_tx[txid] = TxMinedInfo(height=height,
                                                 conf=None,
                                                 timestamp=timestamp,
                                                 txpos=txpos,
                                                 header_hash=header_hash)
        # Transactions pending verification.  txid -> tx_height. Access with self.lock.
        self.unverified_tx = defaultdict(int)
        # true when synchronized
        self.up_to_date = False
        # thread local storage for caching stuff
        self.threadlocal_cache = threading.local()

        self.load_and_cleanup()


    def with_transaction_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.transaction_lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    def load_and_cleanup(self):
        self.load_transactions()
        self.load_local_history()
        self.check_history()
        self.load_unverified_transactions()
        self.remove_local_transactions_we_dont_have()

    def is_mine(self, address):
        return address in self.history

    def get_addresses(self):
        return sorted(self.history.keys())

    def get_address_history(self, addr):
        h = []
        # we need self.transaction_lock but get_tx_height will take self.lock
        # so we need to take that too here, to enforce order of locks
        with self.lock, self.transaction_lock:
            related_txns = self._history_local.get(addr, set())
            for tx_hash in related_txns:
                tx_height = self.get_tx_height(tx_hash).height
                h.append((tx_hash, tx_height))
        return h

    def get_address_history_len(self, addr: str) -> int:
        """Return number of transactions where address is involved."""
        return len(self._history_local.get(addr, ()))

    def get_txin_address(self, txi):
        addr = txi.get('address')
        if addr and addr != "(pubkey)":
            return addr
        prevout_hash = txi.get('prevout_hash')
        prevout_n = txi.get('prevout_n')
        dd = self.txo.get(prevout_hash, {})
        for addr, l in dd.items():
            for n, v, is_cb in l:
                if n == prevout_n:
                    return addr
        return None

    def get_txout_address(self, txo: TxOutput):
        if txo.type == TYPE_ADDRESS:
            addr = txo.address
        elif txo.type == TYPE_PUBKEY:
            addr = bitcoin.public_key_to_p2pkh(bfh(txo.address))
        else:
            addr = None
        return addr

    def load_unverified_transactions(self):
        # review transactions that are in the history
        for addr, hist in self.history.items():
            for tx_hash, tx_height in hist:
                # add it in case it was previously unconfirmed
                self.add_unverified_tx(tx_hash, tx_height)

    def start_network(self, network):
        self.network = network
        if self.network is not None:
            self.synchronizer = Synchronizer(self)
            self.verifier = SPV(self.network, self)

    def stop_threads(self, write_to_disk=True):
        if self.network:
            if self.synchronizer:
                asyncio.run_coroutine_threadsafe(self.synchronizer.stop(), self.network.asyncio_loop)
                self.synchronizer = None
            if self.verifier:
                asyncio.run_coroutine_threadsafe(self.verifier.stop(), self.network.asyncio_loop)
                self.verifier = None
            self.storage.put('stored_height', self.get_local_height())
        if write_to_disk:
            self.save_transactions()
            self.save_verified_tx()
            self.storage.write()

    def add_address(self, address):
        if address not in self.history:
            self.history[address] = []
            self.set_up_to_date(False)
        if self.synchronizer:
            self.synchronizer.add(address)

    def get_conflicting_transactions(self, tx_hash, tx):
        """Returns a set of transaction hashes from the wallet history that are
        directly conflicting with tx, i.e. they have common outpoints being
        spent with tx. If the tx is already in wallet history, that will not be
        reported as a conflict.
        """
        conflicting_txns = set()
        with self.transaction_lock:
            for txin in tx.inputs():
                if txin['type'] == 'coinbase':
                    continue
                prevout_hash = txin['prevout_hash']
                prevout_n = txin['prevout_n']
                spending_tx_hash = self.spent_outpoints[prevout_hash].get(prevout_n)
                if spending_tx_hash is None:
                    continue
                # this outpoint has already been spent, by spending_tx
                assert spending_tx_hash in self.transactions
                conflicting_txns |= {spending_tx_hash}
            if tx_hash in conflicting_txns:
                # this tx is already in history, so it conflicts with itself
                if len(conflicting_txns) > 1:
                    raise Exception('Found conflicting transactions already in wallet history.')
                conflicting_txns -= {tx_hash}
            return conflicting_txns

    def omni_getname(self, property_id):
        if not hasattr(self, 'omni'):
            return ''
        if self.omni_host == '' or self.omni_daemon is None:
            return ''
        try:
            prop = self.omni_daemon.getProperty(property_id)
            res = prop['result']
            name = res['name']
        except:
            name = "token_%d" % property_id
        return name

    def omni_txdata(self, txid, rawtx):

        if not hasattr(self, 'omni'):
            return {}
        if self.omni_host == '' or self.omni_daemon is None:
            return {}
        if rawtx is None:
            return {}

        try:
            val = self.omni_daemon.decodeTransaction(rawtx)
            if val['error']:
                return {}

            res = val['result']
            # if not res['is_mine']:
            #     return {}
            if res['txid'] != txid:
                return {}
            out = dict()
            out['amount'] = res['amount']
            out['sender'] = res['sendingaddress']
            out['reference'] = res['referenceaddress']
            out['name'] = self.omni_getname(res['propertyid'])
            return out
        except:
            return {}

    def add_transaction(self, tx_hash, tx, allow_unrelated=False):
        assert tx_hash, tx_hash
        assert tx, tx
        assert tx.is_complete()
        # assert tx_hash == tx.txid()  # disabled as expensive; test done by Synchronizer.
        # we need self.transaction_lock but get_tx_height will take self.lock
        # so we need to take that too here, to enforce order of locks
        with self.lock, self.transaction_lock:
            # NOTE: returning if tx in self.transactions might seem like a good idea
            # BUT we track is_mine inputs in a txn, and during subsequent calls
            # of add_transaction tx, we might learn of more-and-more inputs of
            # being is_mine, as we roll the gap_limit forward
            is_coinbase = tx.inputs()[0]['type'] == 'coinbase'
            tx_height = self.get_tx_height(tx_hash).height
            if not allow_unrelated:
                # note that during sync, if the transactions are not properly sorted,
                # it could happen that we think tx is unrelated but actually one of the inputs is is_mine.
                # this is the main motivation for allow_unrelated
                is_mine = any([self.is_mine(self.get_txin_address(txin)) for txin in tx.inputs()])
                is_for_me = any([self.is_mine(self.get_txout_address(txo)) for txo in tx.outputs()])
                if not is_mine and not is_for_me:
                    raise UnrelatedTransactionException()
            # Find all conflicting transactions.
            # In case of a conflict,
            #     1. confirmed > mempool > local
            #     2. this new txn has priority over existing ones
            # When this method exits, there must NOT be any conflict, so
            # either keep this txn and remove all conflicting (along with dependencies)
            #     or drop this txn
            conflicting_txns = self.get_conflicting_transactions(tx_hash, tx)
            if conflicting_txns:
                existing_mempool_txn = any(
                    self.get_tx_height(tx_hash2).height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT)
                    for tx_hash2 in conflicting_txns)
                existing_confirmed_txn = any(
                    self.get_tx_height(tx_hash2).height > 0
                    for tx_hash2 in conflicting_txns)
                if existing_confirmed_txn and tx_height <= 0:
                    # this is a non-confirmed tx that conflicts with confirmed txns; drop.
                    return False
                if existing_mempool_txn and tx_height == TX_HEIGHT_LOCAL:
                    # this is a local tx that conflicts with non-local txns; drop.
                    return False
                # keep this txn and remove all conflicting
                to_remove = set()
                to_remove |= conflicting_txns
                for conflicting_tx_hash in conflicting_txns:
                    to_remove |= self.get_depending_transactions(conflicting_tx_hash)
                for tx_hash2 in to_remove:
                    self.remove_transaction(tx_hash2)
            # add inputs
            def add_value_from_prev_output():
                dd = self.txo.get(prevout_hash, {})
                # note: this nested loop takes linear time in num is_mine outputs of prev_tx
                for addr, outputs in dd.items():
                    # note: instead of [(n, v, is_cb), ...]; we could store: {n -> (v, is_cb)}
                    for n, v, is_cb in outputs:
                        if n == prevout_n:
                            if addr and self.is_mine(addr):
                                if d.get(addr) is None:
                                    d[addr] = set()
                                d[addr].add((ser, v))
                            return
            self.txi[tx_hash] = d = {}
            for txi in tx.inputs():
                if txi['type'] == 'coinbase':
                    continue
                prevout_hash = txi['prevout_hash']
                prevout_n = txi['prevout_n']
                ser = prevout_hash + ':%d' % prevout_n
                self.spent_outpoints[prevout_hash][prevout_n] = tx_hash
                add_value_from_prev_output()
            # add outputs
            self.txo[tx_hash] = d = {}
            for n, txo in enumerate(tx.outputs()):
                v = txo[2]
                ser = tx_hash + ':%d'%n
                addr = self.get_txout_address(txo)
                if addr and self.is_mine(addr):
                    if d.get(addr) is None:
                        d[addr] = []
                    d[addr].append((n, v, is_coinbase))
                    # give v to txi that spends me
                    next_tx = self.spent_outpoints[tx_hash].get(n)
                    if next_tx is not None:
                        dd = self.txi.get(next_tx, {})
                        if dd.get(addr) is None:
                            dd[addr] = set()
                        if (ser, v) not in dd[addr]:
                            dd[addr].add((ser, v))
                        self._add_tx_to_local_history(next_tx)
            if hasattr(self, 'omni'):
                # add omni tx data
                if self.omni_host != '':
                    self.omni_tx[tx_hash] = self.omni_txdata(tx_hash, tx.raw)

            # add to local history
            self._add_tx_to_local_history(tx_hash)
            # save
            self.transactions[tx_hash] = tx
            return True

    def remove_transaction(self, tx_hash):
        def remove_from_spent_outpoints():
            # undo spends in spent_outpoints
            if tx is not None:  # if we have the tx, this branch is faster
                for txin in tx.inputs():
                    if txin['type'] == 'coinbase':
                        continue
                    prevout_hash = txin['prevout_hash']
                    prevout_n = txin['prevout_n']
                    self.spent_outpoints[prevout_hash].pop(prevout_n, None)
                    if not self.spent_outpoints[prevout_hash]:
                        self.spent_outpoints.pop(prevout_hash)
            else:  # expensive but always works
                for prevout_hash, d in list(self.spent_outpoints.items()):
                    for prevout_n, spending_txid in d.items():
                        if spending_txid == tx_hash:
                            self.spent_outpoints[prevout_hash].pop(prevout_n, None)
                            if not self.spent_outpoints[prevout_hash]:
                                self.spent_outpoints.pop(prevout_hash)
            # Remove this tx itself; if nothing spends from it.
            # It is not so clear what to do if other txns spend from it, but it will be
            # removed when those other txns are removed.
            if not self.spent_outpoints[tx_hash]:
                self.spent_outpoints.pop(tx_hash)

        with self.transaction_lock:
            self.print_error("removing tx from history", tx_hash)
            tx = self.transactions.pop(tx_hash, None)
            remove_from_spent_outpoints()
            self._remove_tx_from_local_history(tx_hash)
            self.txi.pop(tx_hash, None)
            self.txo.pop(tx_hash, None)

    def get_depending_transactions(self, tx_hash):
        """Returns all (grand-)children of tx_hash in this wallet."""
        children = set()
        for other_hash in self.spent_outpoints[tx_hash].values():
            children.add(other_hash)
            children |= self.get_depending_transactions(other_hash)
        return children

    def receive_tx_callback(self, tx_hash, tx, tx_height):
        self.add_unverified_tx(tx_hash, tx_height)
        self.add_transaction(tx_hash, tx, allow_unrelated=True)

    def receive_history_callback(self, addr, hist, tx_fees):
        with self.lock:
            old_hist = self.get_address_history(addr)
            for tx_hash, height in old_hist:
                if (tx_hash, height) not in hist:
                    # make tx local
                    self.unverified_tx.pop(tx_hash, None)
                    self.verified_tx.pop(tx_hash, None)
                    if self.verifier:
                        self.verifier.remove_spv_proof_for_tx(tx_hash)
            self.history[addr] = hist

        for tx_hash, tx_height in hist:
            # add it in case it was previously unconfirmed
            self.add_unverified_tx(tx_hash, tx_height)
            # if addr is new, we have to recompute txi and txo
            tx = self.transactions.get(tx_hash)
            if tx is None:
                continue
            self.add_transaction(tx_hash, tx, allow_unrelated=True)

        # Store fees
        self.tx_fees.update(tx_fees)

    @profiler
    def load_transactions(self):
        # load txi, txo, tx_fees
        # bookkeeping data of is_mine inputs of transactions
        self.txi = self.storage.get('txi', {})  # txid -> address -> (prev_outpoint, value)
        for txid, d in list(self.txi.items()):
            for addr, lst in d.items():
                self.txi[txid][addr] = set([tuple(x) for x in lst])
        # bookkeeping data of is_mine outputs of transactions
        self.txo = self.storage.get('txo', {})  # txid -> address -> (output_index, value, is_coinbase)
        if hasattr(self, 'omni'):
            # add omni tx data
            if self.omni_host != '':
                self.omni_tx = self.storage.get('omni_tx', {})
        self.tx_fees = self.storage.get('tx_fees', {})
        tx_list = self.storage.get('transactions', {})
        # load transactions
        self.transactions = {}
        for tx_hash, raw in tx_list.items():
            tx = Transaction(raw)
            self.transactions[tx_hash] = tx
            if self.txi.get(tx_hash) is None and self.txo.get(tx_hash) is None:
                self.print_error("removing unreferenced tx", tx_hash)
                self.transactions.pop(tx_hash)
        # load spent_outpoints
        _spent_outpoints = self.storage.get('spent_outpoints', {})
        self.spent_outpoints = defaultdict(dict)
        for prevout_hash, d in _spent_outpoints.items():
            for prevout_n_str, spending_txid in d.items():
                prevout_n = int(prevout_n_str)
                if spending_txid not in self.transactions:
                    continue  # only care about txns we have
                self.spent_outpoints[prevout_hash][prevout_n] = spending_txid

    @profiler
    def load_local_history(self):
        self._history_local = {}  # address -> set(txid)
        self._address_history_changed_events = defaultdict(asyncio.Event)  # address -> Event
        for txid in itertools.chain(self.txi, self.txo):
            self._add_tx_to_local_history(txid)

    @profiler
    def check_history(self):
        save = False
        hist_addrs_mine = list(filter(lambda k: self.is_mine(k), self.history.keys()))
        hist_addrs_not_mine = list(filter(lambda k: not self.is_mine(k), self.history.keys()))
        for addr in hist_addrs_not_mine:
            self.history.pop(addr)
            save = True
        for addr in hist_addrs_mine:
            hist = self.history[addr]
            for tx_hash, tx_height in hist:
                if self.txi.get(tx_hash) or self.txo.get(tx_hash):
                    continue
                tx = self.transactions.get(tx_hash)
                if tx is not None:
                    self.add_transaction(tx_hash, tx, allow_unrelated=True)
                    save = True
        if save:
            self.save_transactions()

    def remove_local_transactions_we_dont_have(self):
        txid_set = set(self.txi) | set(self.txo)
        for txid in txid_set:
            tx_height = self.get_tx_height(txid).height
            if tx_height == TX_HEIGHT_LOCAL and txid not in self.transactions:
                self.remove_transaction(txid)

    @profiler
    def save_transactions(self, write=False):
        with self.transaction_lock:
            tx = {}
            for k,v in self.transactions.items():
                tx[k] = str(v)
            self.storage.put('transactions', tx)
            self.storage.put('txi', self.txi)
            self.storage.put('txo', self.txo)
            if hasattr(self, 'omni'):
                # add omni tx data
                if self.omni_host != '':
                    self.storage.put('omni_tx', self.omni_tx)
            self.storage.put('tx_fees', self.tx_fees)
            self.storage.put('addr_history', self.history)
            self.storage.put('spent_outpoints', self.spent_outpoints)
            if write:
                self.storage.write()

    def save_verified_tx(self, write=False):
        with self.lock:
            verified_tx_to_save = {}
            for txid, tx_info in self.verified_tx.items():
                verified_tx_to_save[txid] = (tx_info.height, tx_info.timestamp,
                                             tx_info.txpos, tx_info.header_hash)
            self.storage.put('verified_tx3', verified_tx_to_save)
            if write:
                self.storage.write()

    def clear_history(self):
        with self.lock:
            with self.transaction_lock:
                self.txi = {}
                self.txo = {}
                self.tx_fees = {}
                self.spent_outpoints = defaultdict(dict)
                self.history = {}
                self.verified_tx = {}
                self.transactions = {}  # type: Dict[str, Transaction]
                self.save_transactions()

    def get_txpos(self, tx_hash):
        """Returns (height, txpos) tuple, even if the tx is unverified."""
        with self.lock:
            if tx_hash in self.verified_tx:
                info = self.verified_tx[tx_hash]
                return info.height, info.txpos
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return (height, 0) if height > 0 else ((1e9 - height), 0)
            else:
                return (1e9+1, 0)

    def with_local_height_cached(func):
        # get local height only once, as it's relatively expensive.
        # take care that nested calls work as expected
        def f(self, *args, **kwargs):
            orig_val = getattr(self.threadlocal_cache, 'local_height', None)
            self.threadlocal_cache.local_height = orig_val or self.get_local_height()
            try:
                return func(self, *args, **kwargs)
            finally:
                self.threadlocal_cache.local_height = orig_val
        return f

    def omni_addr_balance(self, domain):
        total = Decimal(0)
        for addr in domain:
            try:
                val = self.omni_daemon.getBalance(addr, int(self.omni_property))
                res = val['result']
                total += Decimal(res['balance'])
            except:
                pass
        return total

    @with_local_height_cached
    def get_history(self, domain=None):
        # get domain
        if domain is None:
            domain = self.history.keys()
        domain = set(domain)
        # 1. Get the history of each address in the domain, maintain the
        #    delta of a tx as the sum of its deltas on domain addresses
        tx_deltas = defaultdict(int)
        omni_deltas = dict()
        for addr in domain:
            h = self.get_address_history(addr)
            for tx_hash, height in h:
                delta = self.get_tx_delta(tx_hash, addr)
                if delta is None or tx_deltas[tx_hash] is None:
                    tx_deltas[tx_hash] = None
                else:
                    tx_deltas[tx_hash] += delta
                omni_delta = self.get_omni_delta(tx_hash, addr)
                if omni_delta != 0:
                    # safety check and append tx_hash to tx_deltas
                    if not tx_hash in tx_deltas:
                        tx_deltas[tx_hash] = 0
                    if tx_hash in omni_deltas:
                        omni_deltas[tx_hash] += omni_delta
                    else:
                        omni_deltas[tx_hash] = omni_delta
        # 2. create sorted history
        history = []
        for tx_hash in tx_deltas:
            delta = tx_deltas[tx_hash]
            if tx_hash in omni_deltas:
                omni_delta = omni_deltas[tx_hash]
            else:
                omni_delta = 0
            tx_mined_status = self.get_tx_height(tx_hash)
            history.append((tx_hash, tx_mined_status, delta, omni_delta))
        history.sort(key = lambda x: self.get_txpos(x[0]), reverse=True)
        # 3. add balance
        c, u, x = self.get_balance(domain)
        balance = c + u + x
        if hasattr(self, 'omni') and self.omni:
            omni_balance = self.omni_addr_balance(domain)
        else:
            omni_balance = 0
        h2 = []
        for tx_hash, tx_mined_status, delta, omni_delta in history:
            h2.append((tx_hash, tx_mined_status, delta, balance, omni_delta, omni_balance))
            if balance is None or delta is None:
                balance = None
            else:
                balance -= delta
            if (not omni_delta is None) and (omni_delta != 0):
                omni_balance -= omni_delta
        h2.reverse()
        # fixme: this may happen if history is incomplete
        if balance not in [None, 0]:
            self.print_error("Error: history not synchronized")
            return []
        # don't check omni_balance for 0 as
        # omni_balance could be != 0 as in case of direct getting omni from the super-address
        # thare is not any incoming omni tx
        return h2

    def _add_tx_to_local_history(self, txid):
        with self.transaction_lock:
            for addr in itertools.chain(self.txi.get(txid, []), self.txo.get(txid, [])):
                cur_hist = self._history_local.get(addr, set())
                cur_hist.add(txid)
                self._history_local[addr] = cur_hist
                self._mark_address_history_changed(addr)

    def _remove_tx_from_local_history(self, txid):
        with self.transaction_lock:
            for addr in itertools.chain(self.txi.get(txid, []), self.txo.get(txid, [])):
                cur_hist = self._history_local.get(addr, set())
                try:
                    cur_hist.remove(txid)
                except KeyError:
                    pass
                else:
                    self._history_local[addr] = cur_hist

    def _mark_address_history_changed(self, addr: str) -> None:
        # history for this address changed, wake up coroutines:
        self._address_history_changed_events[addr].set()
        # clear event immediately so that coroutines can wait() for the next change:
        self._address_history_changed_events[addr].clear()

    async def wait_for_address_history_to_change(self, addr: str) -> None:
        """Wait until the server tells us about a new transaction related to addr.

        Unconfirmed and confirmed transactions are not distinguished, and so e.g. SPV
        is not taken into account.
        """
        assert self.is_mine(addr), "address needs to be is_mine to be watched"
        await self._address_history_changed_events[addr].wait()

    def add_unverified_tx(self, tx_hash, tx_height):
        if tx_hash in self.verified_tx:
            if tx_height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT):
                with self.lock:
                    self.verified_tx.pop(tx_hash)
                if self.verifier:
                    self.verifier.remove_spv_proof_for_tx(tx_hash)
        else:
            with self.lock:
                # tx will be verified only if height > 0
                self.unverified_tx[tx_hash] = tx_height

    def remove_unverified_tx(self, tx_hash, tx_height):
        with self.lock:
            new_height = self.unverified_tx.get(tx_hash)
            if new_height == tx_height:
                self.unverified_tx.pop(tx_hash, None)

    def add_verified_tx(self, tx_hash: str, info: TxMinedInfo):
        # Remove from the unverified map and add to the verified map
        with self.lock:
            self.unverified_tx.pop(tx_hash, None)
            self.verified_tx[tx_hash] = info
        tx_mined_status = self.get_tx_height(tx_hash)
        self.network.trigger_callback('verified', self, tx_hash, tx_mined_status)

    def get_unverified_txs(self):
        '''Returns a map from tx hash to transaction height'''
        with self.lock:
            return dict(self.unverified_tx)  # copy

    def undo_verifications(self, blockchain, height):
        '''Used by the verifier when a reorg has happened'''
        txs = set()
        with self.lock:
            for tx_hash, info in list(self.verified_tx.items()):
                tx_height = info.height
                if tx_height >= height:
                    header = blockchain.read_header(tx_height)
                    if not header or hash_header(header) != info.header_hash:
                        self.verified_tx.pop(tx_hash, None)
                        # NOTE: we should add these txns to self.unverified_tx,
                        # but with what height?
                        # If on the new fork after the reorg, the txn is at the
                        # same height, we will not get a status update for the
                        # address. If the txn is not mined or at a diff height,
                        # we should get a status update. Unless we put tx into
                        # unverified_tx, it will turn into local. So we put it
                        # into unverified_tx with the old height, and if we get
                        # a status update, that will overwrite it.
                        self.unverified_tx[tx_hash] = tx_height
                        txs.add(tx_hash)
        return txs

    def get_local_height(self):
        """ return last known height if we are offline """
        cached_local_height = getattr(self.threadlocal_cache, 'local_height', None)
        if cached_local_height is not None:
            return cached_local_height
        return self.network.get_local_height() if self.network else self.storage.get('stored_height', 0)

    def get_tx_height(self, tx_hash: str) -> TxMinedInfo:
        with self.lock:
            if tx_hash in self.verified_tx:
                info = self.verified_tx[tx_hash]
                conf = max(self.get_local_height() - info.height + 1, 0)
                return info._replace(conf=conf)
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return TxMinedInfo(height=height, conf=0)
            else:
                # local transaction
                return TxMinedInfo(height=TX_HEIGHT_LOCAL, conf=0)

    def set_up_to_date(self, up_to_date):
        with self.lock:
            self.up_to_date = up_to_date
        if self.network:
            self.network.notify('status')
        if up_to_date:
            self.save_transactions(write=True)
            # if the verifier is also up to date, persist that too;
            # otherwise it will persist its results when it finishes
            if self.verifier and self.verifier.is_up_to_date():
                self.save_verified_tx(write=True)

    def is_up_to_date(self):
        with self.lock: return self.up_to_date

    @with_transaction_lock
    def get_tx_delta(self, tx_hash, address):
        """effect of tx on address"""
        delta = 0
        # substract the value of coins sent from address
        d = self.txi.get(tx_hash, {}).get(address, [])
        for n, v in d:
            delta -= v
        # add the value of the coins received at address
        d = self.txo.get(tx_hash, {}).get(address, [])
        for n, v, cb in d:
            delta += v
        return delta

    @with_transaction_lock
    def get_omni_delta(self, tx_hash, address):
        """effect of tx on address"""
        if not hasattr(self, 'omni'):
            return 0
        # substract the value of coins sent from address
        if not tx_hash in self.omni_tx:
            return 0
        tx_data = self.omni_tx[tx_hash]
        if (not 'amount' in tx_data) or (not 'sender' in tx_data) or (not 'reference' in tx_data):
            return 0
        value = Decimal(tx_data['amount'])
        if tx_data['sender'] == address:
            return -value
        if tx_data['reference'] == address:
            return value
        return 0


    @with_transaction_lock
    def get_tx_value(self, txid):
        """effect of tx on the entire domain"""
        delta = 0
        for addr, d in self.txi.get(txid, {}).items():
            for n, v in d:
                delta -= v
        for addr, d in self.txo.get(txid, {}).items():
            for n, v, cb in d:
                delta += v
        return delta

    def get_wallet_delta(self, tx: Transaction):
        """ effect of tx on wallet """
        is_relevant = False  # "related to wallet?"
        is_mine = False
        is_pruned = False
        is_partial = False
        v_in = v_out = v_out_mine = 0
        for txin in tx.inputs():
            addr = self.get_txin_address(txin)
            if self.is_mine(addr):
                is_mine = True
                is_relevant = True
                d = self.txo.get(txin['prevout_hash'], {}).get(addr, [])
                for n, v, cb in d:
                    if n == txin['prevout_n']:
                        value = v
                        break
                else:
                    value = None
                if value is None:
                    is_pruned = True
                else:
                    v_in += value
            else:
                is_partial = True
        if not is_mine:
            is_partial = False
        for o in tx.outputs():
            v_out += o.value
            if self.is_mine(o.address):
                v_out_mine += o.value
                is_relevant = True
        if is_pruned:
            # some inputs are mine:
            fee = None
            if is_mine:
                v = v_out_mine - v_out
            else:
                # no input is mine
                v = v_out_mine
        else:
            v = v_out_mine - v_in
            if is_partial:
                # some inputs are mine, but not all
                fee = None
            else:
                # all inputs are mine
                fee = v_in - v_out
        if not is_mine:
            fee = None
        return is_relevant, is_mine, v, fee

    def get_tx_fee(self, tx: Transaction) -> Optional[int]:
        if not tx:
            return None
        if hasattr(tx, '_cached_fee'):
            return tx._cached_fee
        with self.lock, self.transaction_lock:
            is_relevant, is_mine, v, fee = self.get_wallet_delta(tx)
            if fee is None:
                txid = tx.txid()
                fee = self.tx_fees.get(txid)
            # only cache non-None, as None can still change while syncing
            if fee is not None:
                tx._cached_fee = fee
        return fee

    def get_addr_io(self, address):
        with self.lock, self.transaction_lock:
            h = self.get_address_history(address)
            received = {}
            sent = {}
            for tx_hash, height in h:
                l = self.txo.get(tx_hash, {}).get(address, [])
                for n, v, is_cb in l:
                    received[tx_hash + ':%d'%n] = (height, v, is_cb)
            for tx_hash, height in h:
                l = self.txi.get(tx_hash, {}).get(address, [])
                for txi, v in l:
                    sent[txi] = height
        return received, sent

    def get_addr_utxo(self, address):
        coins, spent = self.get_addr_io(address)
        for txi in spent:
            coins.pop(txi)
        out = {}
        for txo, v in coins.items():
            tx_height, value, is_cb = v
            prevout_hash, prevout_n = txo.split(':')
            x = {
                'address':address,
                'value':value,
                'prevout_n':int(prevout_n),
                'prevout_hash':prevout_hash,
                'height':tx_height,
                'coinbase':is_cb
            }
            out[txo] = x
        return out

    # return the total amount ever received by an address
    def get_addr_received(self, address):
        received, sent = self.get_addr_io(address)
        return sum([v for height, v, is_cb in received.values()])

    @with_local_height_cached
    def get_addr_balance(self, address):
        """Return the balance of a bitcoin address:
        confirmed and matured, unconfirmed, unmatured
        """
        received, sent = self.get_addr_io(address)
        c = u = x = 0
        local_height = self.get_local_height()
        for txo, (tx_height, v, is_cb) in received.items():
            if is_cb and tx_height + COINBASE_MATURITY > local_height:
                x += v
            elif tx_height > 0:
                c += v
            else:
                u += v
            if txo in sent:
                if sent[txo] > 0:
                    c -= v
                else:
                    u -= v
        return c, u, x

    @with_local_height_cached
    def get_utxos(self, domain=None, excluded=None, mature=False, confirmed_only=False, nonlocal_only=False):
        coins = []
        if domain is None:
            domain = self.get_addresses()
        domain = set(domain)
        if excluded:
            domain = set(domain) - excluded
        for addr in domain:
            utxos = self.get_addr_utxo(addr)
            for x in utxos.values():
                if confirmed_only and x['height'] <= 0:
                    continue
                if nonlocal_only and x['height'] == TX_HEIGHT_LOCAL:
                    continue
                if mature and x['coinbase'] and x['height'] + COINBASE_MATURITY > self.get_local_height():
                    continue
                coins.append(x)
                continue
        return coins

    def get_balance(self, domain=None):
        if domain is None:
            domain = self.get_addresses()
        domain = set(domain)
        cc = uu = xx = 0
        for addr in domain:
            c, u, x = self.get_addr_balance(addr)
            cc += c
            uu += u
            xx += x
        return cc, uu, xx

    def is_used(self, address):
        h = self.history.get(address,[])
        return len(h) != 0

    def is_empty(self, address):
        c, u, x = self.get_addr_balance(address)
        return c+u+x == 0

    def synchronize(self):
        pass
