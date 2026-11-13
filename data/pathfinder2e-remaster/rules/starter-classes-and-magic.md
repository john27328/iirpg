# Starter classes, feats and magic — implementation reference

Pathfinder Second Edition Remaster, Player Core (2023). Checked 2026-11-13
through Archives of Nethys. Licensed mechanics under ORC;
[attribution and unchanged license](../../../src/iirpg/rules/character-ORC-NOTICE.md).
Concise IIRPG paraphrases and implementation notes, not full chapters.

- [Fighter, p. 136](https://2e.aonprd.com/Classes.aspx?ID=35): class HP 10 plus
  Constitution each level; level-one simple/martial weapons and Perception are
  expert. Fortitude and Reflex expert, Will trained. Armor trained.
- [Rogue, p. 166](https://2e.aonprd.com/Classes.aspx?ID=37): class HP 8 plus
  Constitution; trained weapons, expert Perception, Reflex and Will. Agile or
  finesse melee Strikes against off-guard targets add 1d6 precision at level one.
- [Wizard, p. 192](https://2e.aonprd.com/Classes.aspx?ID=39): class HP 6 plus
  Constitution; Intelligence-based arcane spells. Level one provides two ordinary
  prepared rank-one slots and five ordinary cantrips; schools can grant more.
  A prepared spell consumes its slot, cantrips do not. IIRPG implements only
  the ordinary slots as two Force Barrage copies, and three cantrips.
- [Versatile Human, p. 63](https://2e.aonprd.com/Heritages.aspx?ID=262): choose
  a general feat whose prerequisites are met; used as the starter feat slot.
- [Fleet, p. 256](https://2e.aonprd.com/Feats.aspx?ID=5150): Speed +5 feet.
- [Incredible Initiative, p. 256](https://2e.aonprd.com/Feats.aspx?ID=5160):
  +2 circumstance bonus to initiative.
- [Force Barrage, p. 332](https://2e.aonprd.com/Spells.aspx?ID=1536): 1–3 actions,
  120 feet, visible creature targets. Each action creates one shard at rank one,
  automatically dealing 1d4+1 force damage. Combine shards on the same target.
  IIRPG currently directs all shards at one target.
- [Daze, p. 322](https://2e.aonprd.com/Spells.aspx?ID=1482): two-action cantrip,
  60 feet, 1d6 mental damage, basic Will save; critical failure also stunned 1.
  Nonlethal. Higher-rank scaling is outside level-one starter builds.
- [Light, p. 340](https://2e.aonprd.com/Spells.aspx?ID=1585): two-action cantrip;
  an orb shines brightly 20 feet and dimly for another 20, until next daily
  preparations. Can attach to a willing creature. IIRPG supports one caster orb.
- [Detect Magic, p. 323](https://2e.aonprd.com/Spells.aspx?ID=1485): two-action
  cantrip, 30-foot emanation. Rank one reports presence/absence without source
  identification. Known magic can be ignored. This scenario has no unknown magic.

Stored [Playing the Game](playing-the-game.md) supplies basic saves, flanking,
rest and daily preparations; [Conditions](conditions.md) supplies off-guard and
stunned. Basic saves: double/full/half/no damage on critical failure/failure/
success/critical success; halve rounding down. Stunned 1 reduces actions when
next regained. A night's rest heals Constitution modifier (minimum one) × level.

Sample attributes, armor, weapons and incomplete class selections are authored
starter configurations. [Implementation limits](../../../docs/classes-and-magic.md).
