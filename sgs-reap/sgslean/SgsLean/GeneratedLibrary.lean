/- 自动生成：由 SgsLean.Materialize 落盘，请勿手改。 -/
import Mathlib

set_option autoImplicit true

theorem sgs_lem_1 : ∀ (a b : ℝ), Real.logb 8 a + Real.logb 4 (b ^ 2) = 5 → Real.logb 8 a + Real.logb 4 (b ^ 2) = Real.logb 8 a + Real.logb 4 (b ^ 2) := by
  intro a b h
  rfl  -- 来源：cand:aime_1984_p5#0

theorem sgs_lem_2 : ∀ (a b : ℝ), Real.logb 8 a + Real.logb 4 (b ^ 2) = 5 → Real.logb 8 b + Real.logb 4 (a ^ 2) = 7 → Real.logb 8 a + Real.logb 8 b + Real.logb 4 (a ^ 2) + Real.logb 4 (b ^ 2) = 12 := by
  intro a b h1 h2
  linarith  -- 来源：cand:aime_1984_p5#1

theorem sgs_lem_3 : ∀ (x : ℝ) (h₀ : 0 < x), Real.logb 2 x = Real.logb 8 x * 3 := by
  intro x h₀
  simp only [Real.logb]
  rw [show Real.log 8 = Real.log (2 ^ 3) by norm_num]
  rw [Real.log_pow]
  field_simp
  ring  -- 来源：cand:aime_1988_p3#0

theorem sgs_lem_4 : ∀ x y : ℝ, 0 ≤ x → 0 ≤ y → x ^ ((3 : ℝ) / 2) * y ^ ((3 : ℝ) / 2) = (x * y) ^ ((3 : ℝ) / 2) := by
  intro x y hx hy
  have h : (0 : ℝ) ≤ x * y := mul_nonneg hx hy
  rw [← Real.mul_rpow hx hy]  -- 来源：cand:aime_1990_p2#1
