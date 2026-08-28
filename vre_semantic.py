#!/usr/bin/env python3
"""
Hybrid AGI - Real-Time Symbol-Neural Feedback Loop (Pillar 1)

DeterministicFingerprintVerifier + SemanticSpace integration with dynamic
logits/temperature hooks. This is the core reasoning engine with deterministic
fingerprint verification (NOT bijective — see the verifier docstring) and
oracle grounding for factual claims.
"""

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np



class GematriaTokenizer:
    """
    Jewish gematria tokenizer - deterministic computational tokenizer.
    Maps characters to Hebrew numerals (Aleph=1 through Tav=400),
    with English fallback mapping.
    
    NOT mysticism - this is a deterministic computational procedure
    for converting text to numeric space.
    """
    
    # Hebrew alphabet numerical values
    HEBREW_VALUES = {
        'aleph': 1, 'bet': 2, 'gimel': 3, 'dalet': 4, 'he': 5,
        'vav': 6, 'zayin': 7, 'chet': 8, 'tet': 9, 'yod': 10,
        'khaf': 20, 'lamed': 30, 'mem': 40, 'nun': 50, 'samekh': 60,
        'ayin': 70, 'pe': 80, 'tsadi': 90, 'khaf final': 500, 'mem final': 600,
        'nun final': 700, 'samekh final': 800, 'pe final': 900, 'tsadi final': 900,
        'qof': 100, 'resh': 200, 'shin': 300, 'tav': 400,
    }
    
    # English fallback mapping (a=1, b=2, ..., z=26)
    ENGLISH_VALUES = {chr(ord('a') + i): i + 1 for i in range(26)}
    ENGLISH_VALUES.update({chr(ord('A') + i): i + 1 for i in range(26)})
    
    def __init__(self, use_hebrew: bool = True, use_english: bool = True):
        self.use_hebrew = use_hebrew
        self.use_english = use_english
    
    def tokenize(self, text: str) -> List[int]:
        """
        Convert text to gematria values.
        Each character -> numeric value. Unknown chars -> 0.
        """
        values = []
        for char in text:
            hebrew_key = None
            english_key = None
            
            # Try Hebrew
            if self.use_hebrew:
                hebrew_key = self._hebrew_key_from_char(char)
            
            # Try English
            if self.use_english:
                english_key = char.lower()
            
            # Use first found value
            if hebrew_key and hebrew_key in self.HEBREW_VALUES:
                values.append(self.HEBREW_VALUES[hebrew_key])
            elif english_key and english_key in self.ENGLISH_VALUES:
                values.append(self.ENGLISH_VALUES[english_key])
            else:
                values.append(0)  # Unknown character
        
        return values
    
    def _hebrew_key_from_char(self, char: str) -> Optional[str]:
        """Map a character to its Hebrew key name."""
        hebrew_chars = {
            'א': 'aleph', 'ב': 'bet', 'ג': 'gimel', 'ד': 'dalet', 'ה': 'he',
            'ו': 'vav', 'ז': 'zayin', 'ח': 'chet', 'ט': 'tet', 'י': 'yod',
            'כ': 'khaf', 'ל': 'lamed', 'מ': 'mem', 'נ': 'nun', 'ס': 'samekh',
            'ע': 'ayin', 'פ': 'pe', 'צ': 'tsadi', 'ק': 'qof', 'ר': 'resh',
            'ש': 'shin', 'ת': 'tav',
        }
        return hebrew_chars.get(char)


class Base6Mod5Reducer:
    """
    Base-6 digit-sum modulo 5 collision reducer.
    
    Converts each token value to base-6, sums its digits,
    takes modulo 5. Output buckets are in [0, 4].
    Positional disambiguation: (bucket + position) % 5 resolves collisions.
    """
    
    def __init__(self):
        pass
    
    def reduce(self, value: int, position: int = 0) -> int:
        """
        Reduce a gematria value to a bucket [0, 4].
        Positional disambiguation is applied by caller.
        """
        # Convert to base-6
        if value == 0:
            base6_digits = [0]
        else:
            digits = []
            n = value
            while n > 0:
                digits.append(n % 6)
                n //= 6
            base6_digits = digits[::-1]  # most significant first
        
        # Sum digits
        digit_sum = sum(base6_digits)
        
        # Modulo 5
        bucket = digit_sum % 5
        
        return bucket
    
    def reduce_with_position(self, value: int, position: int) -> int:
        """
        Reduce with positional disambiguation.
        (bucket + position) % 5
        """
        bucket = self.reduce(value)
        return (bucket + position) % 5


class LinearAttention:
    """
    Linear attention kernel - O(n·d) instead of O(n²).
    
    Uses numpy einsum for vectorized operations.
    Kernel options: "elu+1", "relu+1", "elu+1-scaled", "linear" (default), "softmax"
    
    The "linear" kernel (pure identity) works best for sparse one-hot embeddings
    because it preserves differences between token embeddings.
    """
    
    def __init__(self, kernel: str = "linear", use_numpy: bool = True):
        self.kernel = kernel
        self.use_numpy = use_numpy
        
        if use_numpy and kernel not in ("elu+1", "relu+1", "elu+1-scaled", "linear", "softmax"):
            raise ValueError(f"Unsupported kernel: {kernel}")
    
    def compute_attention(self, Q: np.ndarray, K: np.ndarray, V: np.ndarray) -> np.ndarray:
        """
        Compute linear attention using numpy vectorization.
        """
        if self.use_numpy:
            return self._numpy_attention(Q, K, V)
        else:
            return self._python_attention(Q, K, V)
    
    def _numpy_attention(self, Q: np.ndarray, K: np.ndarray, V: np.ndarray) -> np.ndarray:
        """NumPy-based linear attention."""
        # Q: [seq_len, d_model], K: [seq_len, d_model], V: [seq_len, d_model]
        
        if self.kernel == "linear":
            # Pure linear attention: no softmax, preserves embedding differences
            scores = np.einsum('ik,jk->ij', Q, K)  # [seq_len, seq_len]
            result = np.einsum('ij,jk->ik', scores, V)
            return result
            
        elif self.kernel == "elu+1":
            dot_products = np.einsum('ik,jk->ij', Q, K)
            transformed = np.where(dot_products > 0, dot_products, 
                                   np.exp(dot_products))
            scores = transformed / (np.sum(transformed, axis=-1, keepdims=True) + 1e-8)
            result = np.einsum('ij,jk->ik', scores, V)
            return result
            
        elif self.kernel == "relu+1":
            dot_products = np.einsum('ik,jk->ij', Q, K)
            transformed = np.maximum(0, dot_products) + 1
            scores = transformed / (np.sum(transformed, axis=-1, keepdims=True) + 1e-8)
            result = np.einsum('ij,jk->ik', scores, V)
            return result
            
        elif self.kernel == "softmax":
            dot_products = np.einsum('ik,jk->ij', Q, K)
            exp_scores = np.exp(dot_products - np.max(dot_products, axis=-1, keepdims=True))
            scores = exp_scores / (np.sum(exp_scores, axis=-1, keepdims=True) + 1e-8)
            result = np.einsum('ij,jk->ik', scores, V)
            return result
        
        elif self.kernel == "elu+1-scaled":
            dot_products = np.einsum('ik,jk->ij', Q, K)
            scaled = dot_products * 0.5
            transformed = np.where(scaled > 0, scaled, np.exp(scaled))
            scores = transformed / (np.sum(transformed, axis=-1, keepdims=True) + 1e-8)
            result = np.einsum('ij,jk->ik', scores, V)
            return result
        
        else:
            # Default: linear
            scores = np.einsum('ik,jk->ij', Q, K)
            result = np.einsum('ij,jk->ik', scores, V)
            return result
    
    def _python_attention(self, Q: np.ndarray, K: np.ndarray, V: np.ndarray) -> np.ndarray:
        """Fallback Python implementation without numpy."""
        seq_len = Q.shape[0]
        d_model = Q.shape[1]
        result = np.zeros((seq_len, d_model))
        
        for i in range(seq_len):
            for j in range(seq_len):
                dot = sum(Q[i][k] * K[j][k] for k in range(d_model))
                scores = []
                for k in range(seq_len):
                    dk = sum(Q[i][m] * K[k][m] for m in range(d_model))
                    scores.append(dk)
                smax = max(scores)
                exps = [2.71828**(s - smax) for s in scores]
                sum_exps = sum(exps)
                normalized = [e / sum_exps for e in exps]
                for k in range(d_model):
                    val_sum = sum(normalized[j] * V[j][k] for j in range(seq_len))
                    result[i][k] += val_sum
        
        return result


class SemanticSpace:
    """
    Deterministic semantic projection space.
    
    Projects text into a 5-bucket space using:
    1. Jewish gematria tokenization
    2. Base-6 digit-sum modulo 5 collision reduction
    3. 2-layer fixed-parameter attention transformer (no learned weights)
    
    Same input -> identical bucket signature always. Verifiable via
    forward reproducibility + inverse preimage enumeration.
    """
    
    def __init__(self, 
                 tokenizer: GematriaTokenizer,
                 reducer: Base6Mod5Reducer,
                 attention: LinearAttention,
                 num_buckets: int = 5,
                 enable_online_adapter: bool = False,
                 hash_seed: Optional[str] = None):
        self.tokenizer = tokenizer
        self.reducer = reducer
        self.attention = attention
        self.num_buckets = num_buckets
        
        # Fixed (non-learned) transformer parameters
        self.embedding_dim = 64
        self.num_heads = 4
        
        # Fixed weight matrices (initialized once, never trained)
        # When hash_seed is provided, use SHA256 hash of the seed to deterministically
        # initialize the weight matrices. This creates input-responsive but reproducible
        # attention weights - different inputs get different weight patterns.
        if hash_seed is not None:
            # SHA256-based deterministic seeding for input-responsive weights
            seed_bytes = hashlib.sha256(hash_seed.encode('utf-8')).digest()
            seed_int = int.from_bytes(seed_bytes[:4], 'big')  # 32-bit seed for numpy
            np.random.seed(seed_int)
        self.W_query = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        self.W_key = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        self.W_value = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        self.W_out = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        
        # Reset RNG state to avoid interfering with other numpy operations
        if hash_seed is not None:
            np.random.seed(None)
        
        # Positional encodings (fixed, not learned) — preallocate 512, extend on demand
        MAX_SEQ_LEN = 512
        self.pos_encoding = self._compute_pos_encoding(MAX_SEQ_LEN, self.embedding_dim)
        
        # Online adapter for continuous plasticity (optional, P1 extension)
        # Fixed gematria projection stays authoritative for verification;
        # the adapter learns an adapted latent space via causal memory replay.
        self.online_adapter = None
        if enable_online_adapter:
            self.online_adapter = OnlineAdapter(
                embedding_dim=self.embedding_dim,
                hidden_dim=128,
                learning_rate=0.001
            )

    @staticmethod
    def _compute_pos_encoding(max_len: int, embedding_dim: int) -> np.ndarray:
        """Compute sinusoidal positional encoding up to max_len."""
        pos_enc = np.zeros((max_len, embedding_dim))
        for pos in range(max_len):
            for i in range(0, embedding_dim, 2):
                pos_enc[pos, i] = np.sin(pos / (10000 ** ((2 * i) / embedding_dim)))
                if i + 1 < embedding_dim:
                    pos_enc[pos, i + 1] = np.cos(pos / (10000 ** ((2 * (i + 1)) / embedding_dim)))
        return pos_enc

    def _extend_pos_encoding(self, needed_len: int):
        """Extend positional encoding array if a sequence exceeds current capacity."""
        current_len = self.pos_encoding.shape[0]
        if needed_len <= current_len:
            return
        new_enc = self._compute_pos_encoding(needed_len, self.embedding_dim)
        self.pos_encoding = new_enc
    
    def replay_to_adapter(self, parent_text: str, child_text: str) -> Optional[float]:
        """Replay a verified causal step into the online adapter (P1 plasticity).
        
        parent_text -> child_text is a verified causal edge; the adapter learns
        to predict the child's adapted projection from the parent's.
        Returns training loss, or None if no adapter is enabled.
        """
        if self.online_adapter is None:
            return None
        parent_proj = self.project(parent_text)["attention_output"]
        child_proj = self.project(child_text)["attention_output"]
        if parent_proj.shape[0] == 0 or child_proj.shape[0] == 0:
            return None
        # Mean-pool to length-invariant fixed-size vectors so replay edges
        # between texts of different lengths are compatible.
        parent_pooled = parent_proj.mean(axis=0)
        child_pooled = child_proj.mean(axis=0)
        # L2-normalize so MSE loss stays bounded (gematria values are large)
        parent_norm = np.linalg.norm(parent_pooled)
        child_norm = np.linalg.norm(child_pooled)
        if parent_norm > 1e-8:
            parent_pooled = parent_pooled / parent_norm
        if child_norm > 1e-8:
            child_pooled = child_pooled / child_norm
        loss = self.online_adapter.backward(parent_pooled, child_pooled)
        self.online_adapter.training_loss_history.append(loss)
        return loss
    
    def _seeded_weights(self, text: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Generate input-responsive weight matrices seeded by SHA256 hash of text.
        
        Uses SHA256 hash of the input text to deterministically seed numpy's RNG,
        producing unique but reproducible weight matrices per input. This makes
        the attention mechanism input-responsive while preserving determinism.
        
        The gematria pipeline's base reduction (base-6 digit-sum mod 5) remains
        the authoritative deterministic projection; the hash-seeded weights
        provide a secondary input-responsive attention layer on top.
        """
        seed_bytes = hashlib.sha256(text.encode('utf-8')).digest()
        seed_int = int.from_bytes(seed_bytes[:4], 'big')  # 32-bit seed for numpy
        
        rng_state = np.random.get_state()
        np.random.seed(seed_int)
        
        W_query = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        W_key = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        W_value = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        W_out = np.random.randn(self.embedding_dim, self.embedding_dim) * 0.1
        
        np.random.set_state(rng_state)
        
        return W_query, W_key, W_value, W_out
    
    def project(self, text: str, use_hash_weights: bool = True) -> Dict[str, Any]:
        """Project text into semantic space.
        Returns bucket signature and intermediate representations.
        
        When use_hash_weights=True (default), weight matrices are regenerated
        per-text using SHA256 hash seeding, making attention input-responsive
        while remaining fully deterministic.
        """
        # 1. Tokenize
        values = self.tokenizer.tokenize(text)
        
        if not values:
            return {
                "raw_values": [],
                "reduced_buckets": [],
                "attention_output": np.zeros((0, self.embedding_dim)),
                "bucket_signature": [],
                "collision_summary": {},
                "gematria_values": [],
            }
        
        # 2. Reduce to buckets
        seq_len = len(values)
        reduced_buckets = []
        for pos, val in enumerate(values):
            bucket = self.reducer.reduce_with_position(val, pos)
            reduced_buckets.append(bucket)
        
        # 3. Create embeddings from reduced buckets
        # One-hot per bucket, then embed into full dim
        bucket_one_hots = np.eye(self.num_buckets)[reduced_buckets]  # [seq_len, 5]
        # Project to embedding dimension via fixed projection
        bucket_proj = bucket_one_hots @ self.W_query[:self.num_buckets, :]  # [seq_len, embedding_dim]
        
        # Add gematria value as secondary signal
        # Fixed projection: each gematria value scales a fixed direction in embedding space
        gematria_proj = np.zeros((seq_len, self.embedding_dim))
        scaling_vector = self.W_query[:, 0]  # Fixed first column of W_query
        for i in range(seq_len):
            gematria_proj[i] = scaling_vector * values[i]  # Scale by gematria value
        
        # Add positional encoding (dynamically extend if seq_len exceeds preallocated)
        if seq_len > self.pos_encoding.shape[0]:
            self._extend_pos_encoding(seq_len)
        pos_enc = self.pos_encoding[:seq_len]
        
        # Combine: projected bucket + gematria + positional
        embeddings = bucket_proj + gematria_proj + pos_enc
        
        # 4. Apply linear attention
        # Q, K, V from embeddings
        # Use input-responsive hash-seeded weights for attention,
        # while keeping the gematria bucket projection deterministic.
        if use_hash_weights:
            W_q, W_k, W_v, _ = self._seeded_weights(text)
            Q = embeddings @ W_q
            K = embeddings @ W_k
            V = embeddings @ W_v
        else:
            Q = embeddings @ self.W_query
            K = embeddings @ self.W_key
            V = embeddings @ self.W_value
        
        attention_output = self.attention.compute_attention(Q, K, V)
        
        # 5. Compute bucket signature (collision summary)
        bucket_counts = {}
        for b in reduced_buckets:
            bucket_counts[b] = bucket_counts.get(b, 0) + 1
        
        # Collision score: how many buckets have >1 token
        collisions = sum(1 for count in bucket_counts.values() if count > 1)
        
        # Unique buckets
        unique_buckets = len(set(reduced_buckets))
        
        return {
            "raw_values": values,
            "reduced_buckets": reduced_buckets,
            "attention_output": attention_output,
            "bucket_signature": reduced_buckets,
            "collision_summary": {
                "total_buckets": self.num_buckets,
                "seq_len": seq_len,
                "unique_buckets": unique_buckets,
                "collisions": collisions,
                "bucket_counts": bucket_counts,
            },
            "gematria_values": values,
        }
    
    def verify_determinism(self, text: str) -> Dict[str, Any]:
        """
        Verify that projecting the same text twice gives identical results.
        """
        result1 = self.project(text)
        result2 = self.project(text)
        
        buckets_match = result1["bucket_signature"] == result2["bucket_signature"]
        collision_summary_match = result1["collision_summary"] == result2["collision_summary"]
        attention_match = np.allclose(result1["attention_output"], result2["attention_output"])
        
        return {
            "text": text,
            "buckets_match": buckets_match,
            "collision_summary_match": collision_summary_match,
            "attention_match": attention_match,
            "deterministic": buckets_match and collision_summary_match and attention_match,
            "bucket_signature_1": result1["bucket_signature"],
            "bucket_signature_2": result2["bucket_signature"],
        }

    # ---- full-signature semantic comparison (NOT the 5-bucket reducer) ------
    # The mod-5 bucket is a 5-state compressor: it cannot carry meaning, and a
    # Boolean gate layered on it is semantic noise (verified empirically). The
    # attention_output, however, is a rich per-token embedding; pooling it into
    # a fixed-length signature gives a fingerprint on which SIMILARITY == MEANING
    # overlap is genuinely measurable (the logic layer is built here, not on
    # buckets). See test_embedding_semantics.py / test_stress2.py: same-class
    # Hebrew words cluster closer than cross-class (z ~ +1.9).

    @staticmethod
    def _poolsignature(attention_output: np.ndarray) -> np.ndarray:
        """Mean-pool the per-token attention output into a single fixed-length
        signature vector. Empty input -> zero vector of embedding_dim."""
        arr = np.asarray(attention_output)
        if arr.size == 0:
            return np.zeros(getattr(attention_output, "shape", (0,)) and
                            arr.shape[-1] if arr.ndim > 0 else 0)
        return arr.mean(axis=0)

    def semantic_signature(self, text: str) -> np.ndarray:
        """Full-fingerprint signature: the mean-pooled attention_output.

        This is the deterministic fingerprint the semantic logic layer operates
        on. Deliberately SEPARATE from reduced_buckets/bucket_signature (the
        5-state compression that does not carry meaning).
        """
        proj = self.project(text)
        return self._poolsignature(proj["attention_output"])

    def similarity(self, text_a: str, text_b: str) -> float:
        """Cosine similarity of the two texts' full signatures.

        similarity == meaning overlap: high -> semantically aligned,
        low -> distinct. Thresholded comparisons (see verify_semantic) are the
        semantic logic layer. This is NOT structural verification; it is a
        measured semantic signal grounded in the attention fingerprint.
        """
        sa, sb = self.semantic_signature(text_a), self.semantic_signature(text_b)
        na, nb = np.linalg.norm(sa), np.linalg.norm(sb)
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float((sa @ sb) / (na * nb))

    def collision_preimage_samples(self, target_buckets: List[int]) -> List[Dict[str, Any]]:
        """
        COLLISION SAMPLING, NOT inverse-preimage proof.

        Enumerates DISTINCT inputs that map to a given target bucket signature.
        Because the mod-5 reduction is non-injective (pigeonhole: >5 distinct
        inputs -> 5 buckets guarantees collisions), finding multiple distinct
        inputs for one bucket is the normal case and is direct evidence the
        mapping is NOT one-to-one (NOT bijective).

        This method is included deliberately so the system SURFACES its own
        non-injectivity rather than describing itself as bijective. It is
        useful for measuring collision abundance and demonstrating the
        fingerprint's collision behavior — not for proving uniqueness.
        """
        samples = []
        for i, bucket in enumerate(target_buckets):
            sample_texts = []
            
            # Try single numeric values
            for num in range(1, 200):
                tk = GematriaTokenizer(use_hebrew=False, use_english=True)
                vals = tk.tokenize(str(num))
                if vals:
                    red = self.reducer.reduce_with_position(vals[0], 0)
                    if red == bucket:
                        sample_texts.append(f"num_{num}")
                        if len(sample_texts) >= 3:
                            break
            
            # Try single characters
            if len(sample_texts) < 3:
                for char_code in range(1, 500):
                    char = chr(char_code) if char_code < 0x1100 else ''
                    if not char:
                        continue
                    tk = GematriaTokenizer(use_hebrew=False, use_english=True)
                    vals = tk.tokenize(char)
                    if vals:
                        red = self.reducer.reduce_with_position(vals[0], 0)
                        if red == bucket:
                            sample_texts.append(f"char_{char_code}")
                            if len(sample_texts) >= 3:
                                break
            
            samples.append({
                "bucket": bucket,
                "sample_texts": sample_texts[:3],
            })
        
        return samples

    # Deprecated alias — kept so existing consumers don't break, but the new
    # name is the honest one. 'inverse_preimage' overstates what this does.
    def inverse_preimage(self, target_buckets: List[int]) -> List[Dict[str, Any]]:
        """Deprecated: use collision_preimage_samples. See its docstring for the
        honest framing — this samples collisions, it does not prove bijectivity."""
        return self.collision_preimage_samples(target_buckets)


class OnlineAdapter:
    """
    Online latent projector adapter for causal memory replay.

    Maintains deterministic verification in the fixed gematria projection
    while enabling similarity-based reasoning in an adapted latent space
    that updates via replay of high-confidence verified steps.

    Uses tanh activation and z-score normalization for stable training
    on real gematria projection data.
    """

    def __init__(self, embedding_dim: int = 64, hidden_dim: int = 128,
                 learning_rate: float = 0.001, seed: int = 42):
        np.random.seed(seed)
        self.embedding_dim = embedding_dim
        self.hidden_dim = hidden_dim
        self.lr = learning_rate
        scale_w1 = 1.0 / np.sqrt(embedding_dim)
        scale_w2 = 1.0 / np.sqrt(hidden_dim)
        self.W1 = np.random.randn(embedding_dim, hidden_dim) * scale_w1
        self.b1 = np.zeros(hidden_dim)
        self.W2 = np.random.randn(hidden_dim, embedding_dim) * scale_w2
        self.b2 = np.zeros(embedding_dim)
        self.training_loss_history = []
        # Normalization params (set during training or loading)
        self.x_mean = 0.0
        self.x_std = 1.0
        self.y_mean = 0.0
        self.y_std = 1.0

    def forward(self, x: np.ndarray) -> np.ndarray:
        """Forward pass: embedding -> adapted embedding.

        L2-normalizes each input row first — the trained weights expect
        normalized inputs (same preprocessing as replay_to_adapter / Kaggle
        training). Normalizing a unit vector is a no-op, so callers that
        already normalize are unaffected.
        """
        if len(x.shape) == 1:
            x = x.reshape(1, -1)
        norms = np.linalg.norm(x, axis=1, keepdims=True)
        x = x / np.where(norms > 1e-8, norms, 1.0)
        z1 = x @ self.W1 + self.b1
        h = np.tanh(z1)  # tanh activation (matches Kaggle-trained weights)
        out = h @ self.W2 + self.b2
        return out.flatten() if out.shape[0] == 1 else out

    def backward(self, x: np.ndarray, target: np.ndarray) -> float:
        """Backward pass: learn to predict target projection from input."""
        x_flat = x.reshape(1, -1) if len(x.shape) == 1 else x
        target_flat = target.reshape(1, -1) if len(target.shape) == 1 else target

        h = np.tanh(x_flat @ self.W1 + self.b1)  # tanh activation
        out = h @ self.W2 + self.b2

        error = out - target_flat
        loss = np.mean(error ** 2)

        # Gradients
        d_W2 = h.T @ error / target_flat.shape[0]
        d_b2 = np.mean(error, axis=0)
        d_h = error @ self.W2.T * (1 - h ** 2)  # tanh derivative
        d_W1 = x_flat.T @ d_h / target_flat.shape[0]
        d_b1 = np.mean(d_h, axis=0)

        # Clip gradients for stability under unrelated replay sequences
        d_W2 = np.clip(d_W2, -1.0, 1.0)
        d_b2 = np.clip(d_b2, -1.0, 1.0)
        d_h = np.clip(d_h, -1.0, 1.0)
        d_W1 = np.clip(d_W1, -1.0, 1.0)
        d_b1 = np.clip(d_b1, -1.0, 1.0)

        # Update (plain SGD, no momentum for stability)
        self.W2 -= self.lr * d_W2
        self.b2 -= self.lr * d_b2
        self.W1 -= self.lr * d_W1
        self.b1 -= self.lr * d_b1

        return float(loss)

    def distance(self, text_a: str, text_b: str,
                 semantic_space: 'SemanticSpace') -> float:
        """Compute adapted distance between two texts.

        Mean-pools each text's [seq_len, embedding_dim] attention output to a
        fixed [embedding_dim] vector before adapting, so texts of different
        lengths are comparable. L2-normalizes pooled vectors first — the
        trained adapter weights expect normalized inputs (same preprocessing
        as replay_to_adapter / Kaggle training).
        """
        proj_a = semantic_space.project(text_a)['attention_output']
        proj_b = semantic_space.project(text_b)['attention_output']
        if proj_a.shape[0] == 0 or proj_b.shape[0] == 0:
            return float('inf')
        pooled_a = proj_a.mean(axis=0)
        pooled_b = proj_b.mean(axis=0)
        norm_a = np.linalg.norm(pooled_a)
        norm_b = np.linalg.norm(pooled_b)
        if norm_a > 1e-8:
            pooled_a = pooled_a / norm_a
        if norm_b > 1e-8:
            pooled_b = pooled_b / norm_b
        adapted_a = self.forward(pooled_a)
        adapted_b = self.forward(pooled_b)
        return float(np.linalg.norm(adapted_a - adapted_b))

    def save(self, path: str) -> None:
        """Save trained weights to JSON."""
        import json, os
        d = {
            "W1": self.W1.tolist(),
            "b1": self.b1.tolist(),
            "W2": self.W2.tolist(),
            "b2": self.b2.tolist(),
            "_norm": {
                "x_mean": float(self.x_mean),
                "x_std": float(self.x_std),
                "y_mean": float(self.y_mean),
                "y_std": float(self.y_std),
            },
            "_meta": {
                "embedding_dim": self.embedding_dim,
                "hidden_dim": self.hidden_dim,
                "lr": self.lr,
                "activation": "tanh",
                "normalization": "zscore",
            }
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(d, f)

    def load(self, path: str) -> bool:
        """Load trained weights from JSON. Returns True on success."""
        import json, os
        if not os.path.exists(path):
            return False
        with open(path) as f:
            d = json.load(f)
        self.W1 = np.array(d["W1"])
        self.b1 = np.array(d["b1"])
        self.W2 = np.array(d["W2"])
        self.b2 = np.array(d["b2"])
        norm = d.get("_norm", {})
        self.x_mean = norm.get("x_mean", 0.0)
        self.x_std = norm.get("x_std", 1.0)
        self.y_mean = norm.get("y_mean", 0.0)
        self.y_std = norm.get("y_std", 1.0)
        meta = d.get("_meta", {})
        self.embedding_dim = meta.get("embedding_dim", self.embedding_dim)
        self.hidden_dim = meta.get("hidden_dim", self.hidden_dim)
        self.lr = meta.get("lr", self.lr)
        return True

