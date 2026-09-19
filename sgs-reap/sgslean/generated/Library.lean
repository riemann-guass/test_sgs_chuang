/- 自动生成：由 SgsLean.Materialize 落盘，请勿手改。 -/
import Mathlib

set_option autoImplicit true

theorem sgs_lem_1 : ∀ (n : Nat), n + 0 = n := by
  intro n
  rfl  -- 来源：g1:g01

theorem sgs_lem_2 : ∀ (n : Nat), n * 0 = 0 := by
  intro n
  rfl  -- 来源：g1:g02

theorem sgs_lem_3 : ∀ (n : Nat), 0 + n = n := by
  intro n
  simp  -- 来源：g1:g03
