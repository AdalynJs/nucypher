import asyncio
import binascii

import msgpack

from nkms.characters import Alice, Bob, Ursula
from nkms.crypto import api
from nkms.crypto.api import keccak_digest
from nkms.crypto.constants import NOT_SIGNED
from nkms.crypto.fragments import KFrag, PFrag
from nkms.crypto.powers import EncryptingPower
from nkms.crypto.signature import Signature
from nkms.crypto.utils import BytestringSplitter
from nkms.keystore.keypairs import PublicKey
from npre.constants import UNKNOWN_KFRAG

group_payload_splitter = BytestringSplitter(PublicKey)
policy_payload_splitter = BytestringSplitter(KFrag)


class Contract(object):
    """
    A Policy must be implemented by agreement with n Ursulas.  This class tracks the status of that implementation.
    """

    def __init__(self, alice, expiration, deposit=None, ursula=None, kfrag=UNKNOWN_KFRAG, alices_signature=None,
                 encrypted_challenge_pack=None):
        """
        :param deposit: Funds which will pay for the timeframe  of this Contract (not the actual re-encryptions);
            a portion will be locked for each Ursula that accepts.
        :param expiration: The moment which Alice wants the Contract to end.
        """
        self.alice = alice
        self.expiration = expiration
        self.deposit = deposit
        self.ursula = ursula
        self.kfrag = kfrag
        self.encrypted_challenge_pack = encrypted_challenge_pack

    def activate(self, kfrag, ursula, negotiation_result):
        self.kfrag = kfrag
        self.ursula = ursula
        self.negotiation_result = negotiation_result

    def encrypt_payload_for_ursula(self):
        """
        Craft an offer to send to Ursula.
        """
        return self.alice.encrypt_for(self.ursula, self.payload())[0]  # We don't need the signature separately.

    def payload(self):
        # TODO: Ship the expiration again?  Or some other way of alerting Ursula to recall her previous dialogue regarding this Contract.
        return bytes(self.kfrag) + b"This might be a ChallengePack"  # TODO: come to a decision re: #146

    @classmethod
    def from_ursula(cls, group_payload, ursula):
        alice_pubkey_sig, payload_encrypted_for_ursula = group_payload_splitter(group_payload,
                                                                                msgpack_remainder=True)
        alice = Alice.from_pubkey_sig_bytes(alice_pubkey_sig)
        ursula.learn_about_actor(alice)
        verified, cleartext = ursula.verify_from(alice, payload_encrypted_for_ursula,
                                                 decrypt=True, signature_is_on_cleartext=True)

        if not verified:
            # TODO: What do we do if it's not signed properly?
            pass

        alices_signature, policy_payload = BytestringSplitter(Signature)(cleartext, return_remainder=True)

        kfrag, encrypted_challenge_pack = policy_payload_splitter(policy_payload, return_remainder=True)

        # TODO: Query stored Contract and reconstitute

        contract = cls(alice=alice, alices_signature=alices_signature, kfrag=kfrag,
                       encrypted_challenge_pack=encrypted_challenge_pack)

        return contract


class PolicyOfferResponse(object):
    pass


class PolicyManager(object):
    pass


class PolicyManagerForAlice(PolicyManager):
    def __init__(self, owner: Alice) -> None:
        self.owner = owner

    def create_policy_group(self,
                            bob: Bob,
                            uri: bytes,
                            m: int,
                            n: int,
                            ):
        """
        Alice dictates a new group of policies.
        """

        ##### Temporary until we decide on an API for private key access
        alice_priv_enc = self.owner._crypto_power._power_ups[EncryptingPower].priv_key
        kfrags, pfrag = self.owner.generate_rekey_frags(alice_priv_enc, bob, m,
                                                        n)  # TODO: Access Alice's private key inside this method.
        policy = Policy.from_alice(
            alice=self.owner,
            bob=bob,
            kfrags=kfrags,
            pfrag=pfrag,
            uri=uri,
        )

        return policy


class PolicyGroup(object):
    """
    The terms and conditions by which Alice shares with Bob.
    """

    _id = None

    def __init__(self, uri: bytes, alice: Alice, bob: Bob, policies=None) -> None:
        self.policies = policies or []
        self.alice = alice
        self.bob = bob
        self.uri = uri
        self.treasure_map = TreasureMap()

    @staticmethod
    def hash(message):
        return keccak_digest(message)

    # TODO: This is a stand-in; remove it.
    @property
    def pfrag(self):
        return self.policies[0].pfrag

    def treasure_map_dht_key(self):
        """
        We need a key that Bob can glean from knowledge he already has *and* which Ursula can verify came from us.
        Ursula will refuse to propagate this key if it she can't prove that our public key, which is included in it,
        was used to sign the payload.

        Our public key (which everybody knows) and the hrac above.
        """
        return self.hash(bytes(self.alice.seal) + self.hrac())

    @property
    def id(self):
        if not self._id:
            self._id = api.keccak_digest(bytes(self.alice.seal), api.keccak_digest(self.uri))
        return self._id

    def publish_treasure_map(self):
        encrypted_treasure_map, signature_for_bob = self.alice.encrypt_for(self.bob,
                                                                           self.treasure_map.packed_payload())
        signature_for_ursula = self.alice.seal(self.hrac())  # TODO: Great use-case for Ciphertext class

        # In order to know this is safe to propagate, Ursula needs to see a signature, our public key,
        # and, reasons explained in treasure_map_dht_key above, the uri_hash.
        dht_value = signature_for_ursula + self.alice.seal + self.hrac() + msgpack.dumps(
            encrypted_treasure_map)  # TODO: Ideally, this is a Ciphertext object instead of msgpack (see #112)
        dht_key = self.treasure_map_dht_key()

        setter = self.alice.server.set(dht_key, b"trmap" + dht_value)
        event_loop = asyncio.get_event_loop()
        event_loop.run_until_complete(setter)
        return encrypted_treasure_map, dht_value, signature_for_bob, signature_for_ursula


class Policy(object):
    """
    An individual agreement between Alice and Ursula.  Together, all of the Policies by which
    Ursula nodes which enter into an agreement regarding the same series of kFrags constitute
    a PolicyGroup.

    A Policy has a unique ID, which includes a fingerprint of Alice's public key so that
    only she can set a policy with that ID.  Ursula must verify this; otherwise a collision
    attack is possible.
    """
    _ursula = None
    hashed_part = None

    def __init__(self, alice, bob=None, kfrags=(UNKNOWN_KFRAG,), pfrag=None, uri=None, alices_signature=NOT_SIGNED,
                 challenge_size=20,
                 encrypted_challenge_pack=None):
        """
        :param kfrag:
            The kFrag obviously, but defaults to UNKNOWN_KFRAG in case the user wants to set it later.
        :param deterministic_id_portion:  Probably the fingerprint of Alice's public key.
            Any part that Ursula can use to verify that Alice is the rightful setter of this ID.
            If it's not included, the Policy ID will be completely random.
        :param challenge_size:  The number of challenges to create in the ChallengePack.
        """
        self.alice = alice
        self.bob = bob
        self.alices_signature = alices_signature
        self.kfrags = kfrags
        self.pfrag = pfrag
        self.uri = uri
        self.random_id_portion = api.secure_random(32)  # TOOD: Where do we actually want this to live?
        self.challenge_size = challenge_size
        self.treasure_map = []
        self.challenge_pack = []
        self._active_contracts = {}

        self._encrypted_challenge_pack = encrypted_challenge_pack

    @property
    def n(self):
        return len(self.kfrags)

    @property
    def ursula(self):
        if not self._ursula:
            raise Ursula.NotFound
        else:
            return self._ursula

    @ursula.setter
    def ursula(self, ursula_object):
        self.alice.learn_about_actor(ursula_object)
        self._ursula = ursula_object

    @staticmethod
    def from_alice(kfrags,
                   pfrag,
                   alice,
                   bob,
                   uri,
                   ):
        policy = Policy(alice, bob, kfrags, pfrag, uri)

        return policy

    def hrac(self):
        """
        A convenience method for generating an hrac for this instance.
        """
        return self.hrac_for(self.alice, self.bob, self.uri)

    @staticmethod
    def hrac_for(alice, bob, uri):

        """
        The "hashed resource authentication code".

        A hash of:
        * Alice's public key
        * Bob's public key
        * the uri

        Alice and Bob have all the information they need to construct this.
        Ursula does not, so we share it with her.
        """
        return PolicyGroup.hash(bytes(alice.seal) + bytes(bob.seal) + uri)

    def enact(self, networky_stuff):

        for kfrag in self.kfrags:
            contract = self._active_contracts[kfrag]
            policy_payload = contract.encrypt_payload_for_ursula()
            full_payload = self.alice.seal + msgpack.dumps(policy_payload)
            response = networky_stuff.enact_policy(contract.ursula,
                                                   self.hrac(),
                                                   full_payload)  # TODO: Parse response for confirmation.

            # Assuming response is what we hope for
            self.treasure_map.add_ursula(policy.ursula)

    @property
    def encrypted_challenge_pack(self):
        if not self._encrypted_challenge_pack:
            if not self.bob:
                raise TypeError("This Policy doesn't have a Bob, so there's no way to encrypt a ChallengePack for Bob.")
            else:
                self._encrypted_challenge_pack = self.alice.encrypt_for(self.bob, msgpack.dumps(self.challenge_pack))
        return self._encrypted_challenge_pack

    def craft_offer(self, deposit, expiration):
        return Contract(self.alice, deposit, expiration)

    def find_n_ursulas(self, networky_stuff, contract: Contract):
        # TODO: This is a number mismatch - we need not one contract, but n contracts.
        """
        :param networky_stuff: A compliant interface (maybe a Client instance) to be used to engage the DHT swarm.
        """
        for kfrag in self.kfrags:
            try:
                ursula, result = networky_stuff.find_ursula(contract)
                # TODO: Here, we need to assess the result and see if we're actually good to go.
                if result.was_accepted:
                    contract.activate(kfrag, ursula, result)
                    self._active_contracts[kfrag] = contract
            except networky_stuff.NotEnoughQualifiedUrsulas:
                pass  # TODO: Tell Alice to either wait or lower the value of n.


class TreasureMap(object):
    def __init__(self, ursula_interface_ids=None):
        self.ids = ursula_interface_ids or []

    def packed_payload(self):
        return msgpack.dumps(self.ids)

    def add_ursula(self, ursula):
        self.ids.append(ursula.interface_dht_key())

    def __eq__(self, other):
        return self.ids == other.ids

    def __iter__(self):
        return iter(self.ids)

    def __len__(self):
        return len(self.ids)


class WorkOrder(object):
    def __init__(self, bob, kfrag_hrac, pfrags, receipt_bytes, receipt_signature, ursula_id=None):
        self.bob = bob
        self.kfrag_hrac = kfrag_hrac
        self.pfrags = pfrags
        self.receipt_bytes = receipt_bytes
        self.receipt_signature = receipt_signature
        self.ursula_id = ursula_id  # TODO: We may still need a more elegant system for ID'ing Ursula.  See #136.

    def __repr__(self):
        return "WorkOrder (pfrags: {}) {} for {}".format([binascii.hexlify(bytes(p))[:6] for p in self.pfrags],
                                                         binascii.hexlify(self.receipt_bytes)[:6],
                                                         binascii.hexlify(self.ursula_id)[:6])

    def __eq__(self, other):
        return (self.receipt_bytes, self.receipt_signature) == (other.receipt_bytes, other.receipt_signature)

    def __len__(self):
        return len(self.pfrags)

    @classmethod
    def constructed_by_bob(cls, kfrag_hrac, pfrags, ursula_dht_key, bob):
        receipt_bytes = b"wo:" + ursula_dht_key  # TODO: represent the pfrags as bytes and hash them as part of the receipt, ie  + keccak_digest(b"".join(pfrags))  - See #137
        receipt_signature = bob.seal(receipt_bytes)
        return cls(bob, kfrag_hrac, pfrags, receipt_bytes, receipt_signature, ursula_dht_key)

    @classmethod
    def from_rest_payload(cls, kfrag_hrac, rest_payload):
        payload_splitter = BytestringSplitter(Signature, PublicKey)
        signature, bob_pubkey_sig, (receipt_bytes, packed_pfrags) = payload_splitter(rest_payload,
                                                                                     msgpack_remainder=True)
        pfrags = [PFrag(p) for p in msgpack.loads(packed_pfrags)]
        verified = signature.verify(receipt_bytes, bob_pubkey_sig)
        if not verified:
            raise ValueError("This doesn't appear to be from Bob.")
        bob = Bob.from_pubkey_sig_bytes(bob_pubkey_sig)
        return cls(bob, kfrag_hrac, pfrags, receipt_bytes, signature)

    def payload(self):
        pfrags_as_bytes = [bytes(p) for p in self.pfrags]
        packed_receipt_and_pfrags = msgpack.dumps((self.receipt_bytes, msgpack.dumps(pfrags_as_bytes)))
        return bytes(self.receipt_signature) + self.bob.seal + packed_receipt_and_pfrags

    def complete(self, cfrags):
        # TODO: Verify that this is in fact complete - right of CFrags and properly signed.
        # TODO: Mark it complete with datetime.
        self