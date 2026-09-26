/- 自动生成：由 SgsLean.Materialize 落盘，请勿手改。 -/
import Mathlib

set_option autoImplicit true

-- 来源：round0:C2:ENNReal.HolderConjugate.inv_one_sub_inv'#1
theorem sgs_lem_f511aded : ∀ {a : ENNReal}, 1 ≤ a → a⁻¹ ≤ 1 := by
  intro a ha
  rw [ENNReal.inv_le_one]
  exact ha

-- 来源：round0:C2:ENNReal.HolderConjugate.toNNReal#0
theorem sgs_lem_c7a019b0 : ∀ {p q : ENNReal} [p.HolderConjugate q], p ≠ 0 → p ≠ ⊤ → p.toNNReal ≠ 0 := by
  intro p q _ hp0 hp_top h
  apply hp0
  have hcoe : (p.toNNReal : ENNReal) = p := ENNReal.coe_toNNReal hp_top
  rw [← hcoe, h]
  rfl

-- 来源：round0:C2:Finset.range_sdiff_zero#1
theorem sgs_lem_d8e3b24e : ∀ (n : ℕ), (Finset.range (n + 1) \ {0}) = (Finset.range (n + 1)).filter (fun k => k ≠ 0) := by
  intro n
  ext k
  simp

-- 来源：round0:C2:Int.ModEq.dvd#1
theorem sgs_lem_4545a0a2 : ∀ {n a b : ℤ}, n ∣ a - b → n ∣ b - a := by
  intro n a b h
  have h' : n ∣ -(a - b) := dvd_neg.mpr h
  simpa [neg_sub] using h'

-- 来源：round0:C2:Int.ediv_ediv_eq_ediv_mul#0
theorem sgs_lem_83cc6bce : ∀ {x y : ℤ}, 0 ≤ y → x / y = x / (1 * y) := by
  intro x y hy
  rw [one_mul]

-- 来源：round0:C2:Int.fib_two_mul#1
theorem sgs_lem_8d73950b : ∀ (n : ℤ), Int.fib (n + 2) = Int.fib n + Int.fib (n + 1) := by
  intro n
  rw [Int.fib_add_two]

-- 来源：round0:C2:Int.gcd_eq_gcd_ab#1
theorem sgs_lem_bf5545b0 : ∀ (x y : ℤ), (x.gcd y : ℤ) ∣ x ∧ (x.gcd y : ℤ) ∣ y := by
  intro x y
  constructor
  · exact Int.gcd_dvd_left x y
  · exact Int.gcd_dvd_right x y

-- 来源：round0:C2:Int.ModEq.sub_right#1
theorem sgs_lem_9835ce25 : ∀ {n a b c : ℤ}, n ∣ a - b → n ∣ (a - c) - (b - c) := by
  intro n a b c h
  rw [sub_sub_sub_cancel_right]
  exact h

-- 来源：round0:C2:Nat.AtLeastTwo.neZero_sub_one#0
theorem sgs_lem_819c12ca : ∀ (n : ℕ), 2 ≤ n → n ≠ 0 := by
  intro n h
  omega

-- 来源：round0:C2:Nat.AtLeastTwo.neZero_sub_one#1
theorem sgs_lem_07536f2e : ∀ (n : ℕ), 2 ≤ n → n - 1 ≠ 0 := by
  intro n h
  omega

-- 来源：round0:C2:ENNReal.HolderConjugate.toNNReal#0
theorem sgs_lem_5061343e : ∀ {p : ENNReal}, 1 < p.toNNReal → p ≠ 0 := by
  intro p hp h
  rw [h] at hp
  simp at hp

-- 来源：round0:C2:ENNReal.HolderConjugate.toNNReal#1
theorem sgs_lem_9b01ac0a : ∀ {p : ENNReal}, 1 < p.toNNReal → p ≠ ⊤ := by
  intro p hp h
  subst h
  norm_num at hp

-- 来源：round0:C2:Int.ModEq.mul_left#0
theorem sgs_lem_793d012c : ∀ {n a b c : ℤ}, a ≡ b [ZMOD n] → n ∣ (b - a) := by
  intro n a b c h
  exact Int.ModEq.dvd h

-- 来源：round0:C2:Int.div_le_div_iff_of_dvd_of_neg_of_neg#1
theorem sgs_lem_70b55335 : ∀ {a b c d : ℤ}, b < 0 → d < 0 → b ∣ a → d ∣ c → 0 < b * d := by
  intro a b c d hb hd hba hdc
  exact mul_pos_of_neg_of_neg hb hd

-- 来源：round0:C2:Int.eq_of_mod_eq_of_natAbs_sub_lt_natAbs#0
theorem sgs_lem_4655447d : ∀ {a b : ℤ}, a % b = a → (a - a).natAbs < b.natAbs → a = a := by
  intro a b h _
  rfl

-- 来源：round0:C2:Int.eq_of_mod_eq_of_natAbs_sub_lt_natAbs#1
theorem sgs_lem_ff259be1 : ∀ {a b c : ℤ}, a % b = c → (a - c).natAbs = 0 → a = c := by
  intro a b c hmod h
  have hz : a - c = 0 := Int.natAbs_eq_zero.mp h
  omega

-- 来源：round1:C2:ENNReal.HolderConjugate.inv_one_sub_inv'#0
theorem sgs_lem_81330356 : ∀ {a : ENNReal}, 1 ≤ a → a ≠ 0 := by
  intro a h
  exact fun h0 => by
    rw [h0] at h
    simp at h

-- 来源：round1:C2:ENNReal.HolderTriple.of_toReal#0
theorem sgs_lem_40c2391e : ∀ {p q r : ENNReal}, p.toReal.HolderTriple q.toReal r.toReal → p ≠ ⊤ → q ≠ ⊤ → r ≠ ⊤ → p.toReal = p.toNNReal := by
  intro p q r h hp hq hr
  have htop : p ≠ ⊤ := hp
  cases p with
  | top => contradiction
  | coe x =>
    simp [ENNReal.toReal, ENNReal.toNNReal]

-- 来源：round1:C2:ENNReal.HolderTriple.of_toReal#1
theorem sgs_lem_1c750afa : ∀ {p q r : ENNReal}, p.toReal.HolderTriple q.toReal r.toReal → p.toReal ≠ 0 → q.toReal ≠ 0 → r.toReal ≠ 0 → p ≠ 0 ∧ q ≠ 0 ∧ r ≠ 0 := by
  intro p q r h hpq hqr hrp
  simp only [ne_eq, ENNReal.toReal_eq_zero_iff] at hpq hqr hrp
  constructor
  · intro hp
    exact hpq (Or.inl hp)
  constructor
  · intro hq
    exact hqr (Or.inl hq)
  · intro hr
    exact hrp (Or.inl hr)

-- 来源：round1:C2:Int.Ioc_filter_modEq_card#0
theorem sgs_lem_6ae03c40 : ∀ (a b v r : ℤ), 0 < r → {x ∈ Finset.Ioc a b | x ≡ v [ZMOD r]}.card = {x ∈ Finset.Ioc a b | x ≡ v [ZMOD r]}.card := by
  intro a b v r hr
  rfl

-- 来源：round1:C2:Int.ModEq.mul_right_cancel'#0
theorem sgs_lem_44a2aa72 : ∀ {m c a b : ℤ}, c ≠ 0 → m * c ∣ a * c - b * c → m ∣ a - b := by
  intro m c a b hc h
  have h1 : a * c - b * c = (a - b) * c := by ring
  rw [h1] at h
  exact (mul_dvd_mul_iff_right hc).mp h

-- 来源：round1:C2:Int.ModEq.mul_right_cancel'#1
theorem sgs_lem_9aafe61f : ∀ {m c a b : ℤ}, c ≠ 0 → a * c ≡ b * c [ZMOD m * c] → a * c - b * c = (a - b) * c := by
  intro m c a b hc h
  ring

-- 来源：round1:C2:Int.ModEq.of_mul_right#1
theorem sgs_lem_3e807de3 : ∀ {n m a b : ℤ}, (n * m) ∣ a - b → n ∣ a - b := by
  intro n m a b h
  rcases h with ⟨k, hk⟩
  use m * k
  rw [hk]
  ring

-- 来源：round1:C2:Int.csSup_eq_greatestOfBdd#0
theorem sgs_lem_ee30b67f : ∀ {s : Set ℤ} [inst : DecidablePred fun x => x ∈ s] (b : ℤ) (Hb : ∀ z ∈ s, z ≤ b) (Hinh : ∃ z, z ∈ s), b ∈ upperBounds s := by
  intro s inst b Hb Hinh
  exact fun z hz => Hb z hz

-- 来源：round1:C2:Int.div_lt_div_iff_of_dvd_of_neg_of_neg#0
theorem sgs_lem_30c6be1b : ∀ {a b : ℤ}, b < 0 → b ∣ a → 0 ≤ a / b → a ≤ 0 := by
  intro a b hb hdiv ha
  obtain ⟨k, hk⟩ := hdiv
  have hbne : b ≠ 0 := by
    intro h
    rw [h] at hb
    exact (lt_irrefl 0) hb
  have hquot : a / b = k := by
    rw [hk]
    exact Int.mul_ediv_cancel_left k hbne
  have hk_nonneg : 0 ≤ k := by
    rw [hquot] at ha
    exact ha
  rw [hk]
  nlinarith

-- 来源：round1:C2:Int.eq_of_mod_eq_of_natAbs_sub_lt_natAbs#1
theorem sgs_lem_cea31a40 : ∀ {a b c : ℤ}, a % b = c → (a - c).natAbs = 0 ∨ (a - c).natAbs ≥ b.natAbs := by
  intro a b c h
  have h1 : ∃ k : ℤ, a = b * k + c := by
    use a / b
    have := Int.emod_add_ediv a b
    omega
  obtain ⟨k, hk⟩ := h1
  have h2 : a - c = b * k := by omega
  rw [h2]
  by_cases hb : b = 0
  · subst hb
    simp
  · have : (b * k).natAbs = b.natAbs * k.natAbs := Int.natAbs_mul b k
    rw [this]
    by_cases hk0 : k = 0
    · subst hk0
      simp
    · right
      have : 0 < k.natAbs := by
        apply Nat.pos_of_ne_zero
        intro hc
        apply hk0
        exact Int.natAbs_eq_zero.mp hc
      calc b.natAbs * k.natAbs ≥ b.natAbs * 1 := by
            exact Nat.mul_le_mul_left _ this
        _ = b.natAbs := by ring

-- 来源：round1:C2:Int.fib_two_mul#1
theorem sgs_lem_8551f841 : ∀ (n : ℤ), Int.fib (n + n) = Int.fib n * (2 * Int.fib (n + 1) - Int.fib n) := by
  intro n
  have h : n + n = 2 * n := by ring
  rw [h, Int.fib_two_mul]

-- 来源：round1:C2:Int.gcd_a_modEq#1
theorem sgs_lem_b149edb3 : ∀ (a b : ℕ), (a.gcd b : ℤ) ∣ (a : ℤ) * a.gcdA b := by
  intro a b
  have h : (a.gcd b : ℤ) ∣ (a : ℤ) := by
    exact_mod_cast Nat.gcd_dvd_left a b
  exact dvd_trans h (dvd_mul_right (a : ℤ) (a.gcdA b))

-- 来源：round2:C2:Nat.AtLeastTwo.neZero_sub_one#0
theorem sgs_lem_cb056ef0 : ∀ (n : ℕ) [n.AtLeastTwo], 1 < n := by
  intro n
  exact Nat.AtLeastTwo.one_lt

-- 来源：round2:C2:Int.ModEq.mul_left#1
theorem sgs_lem_657e23a8 : ∀ {n d c : ℤ}, n ∣ d → n ∣ c * d := by
  intro n d c h
  rcases h with ⟨k, rfl⟩
  exact ⟨c * k, by ring⟩

-- 来源：round2:C2:Int.ModEq.mul_right_cancel'#1
theorem sgs_lem_3093d011 : ∀ {m c a b : ℤ}, c ≠ 0 → (m * c) ∣ (a - b) * c → m ∣ (a - b) := by
  intro m c a b hc h
  have h' : c ∣ (a - b) * c := dvd_trans (dvd_mul_left c m) h
  exact (mul_dvd_mul_iff_right hc).mp h

-- 来源：round0:C2:Dvd.dvd.modEq_zero_int#0
theorem sgs_lem_a346442f : ∀ {n a : ℤ}, n ∣ a → n ∣ a - 0 := by
  intro n a h
  rw [sub_zero]
  exact h

-- 来源：round0:C2:Dvd.dvd.modEq_zero_int#1
theorem sgs_lem_99a2b96f : ∀ {n a : ℤ}, n ∣ a → ∃ k : ℤ, a = n * k := by
  intro n a h
  exact h

-- 来源：round0:C2:Multiset.range_subset#0
theorem sgs_lem_9a6c1f33 : ∀ {m n : ℕ}, m ≤ n → Multiset.range m ⊆ Multiset.range n := by
  intro m n h x hx
  simp only [Multiset.mem_range] at hx ⊢
  exact lt_of_lt_of_le hx h

-- 来源：round0:C2:Nat.AtLeastTwo.neZero_sub_one#1
theorem sgs_lem_46bb794a : ∀ (n : ℕ), n - 1 ≠ 0 → NeZero (n - 1) := by
  intro n h
  exact ⟨h⟩
