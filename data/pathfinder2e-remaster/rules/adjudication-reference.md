<!-- Edition: PF2e Remaster | License: ORC | Checked: 2026-11-13 -->
<!-- Attribution and license: ../ORC-NOTICE.md and ../LICENSE-ORC.pdf -->

# General adjudication reference

An IIRPG paraphrase and source index, not a full rules chapter or executable
implementation. Source works: Player Core (2023) and GM Core (2023).

## Unusual actions

[Adjudicating Rules, GM Core p. 15](https://2e.aonprd.com/Rules.aspx?ID=2497):
creative play needs consistent rulings. Prior rulings should inform similar
situations. Prefer the closest defined action, with a recorded adjudication
where interpretation is necessary.

[The Basics, GM Core p. 15](https://2e.aonprd.com/Rules.aspx?ID=2498): an
undefined quick task generally costs one action, or two when repetition three
times per round would be inappropriate. Assign relevant traits. Opposed tasks
normally use one roll against the other side's DC, rather than two opposed
rolls; initiative is an exception. Select an appropriate skill and DC. A small
circumstantial advantage/disadvantage can be represented by +1/−1. Seek
consequences that let failed attempts advance the situation.

[Adjudicating Actions, GM Core p. 27](https://2e.aonprd.com/Rules.aspx?ID=2551):
normally explain a known obstacle to an action before resources are spent.
Unknown facts can be exceptions; validation must not reveal concealed truths.
Aid needs suitable preparation, position and communication for the actual task.

[Saying “Yes, But”, GM Core p. 16](https://2e.aonprd.com/Rules.aspx?ID=2504):
allow creative approaches through suitable costs, checks and limited effects.
A situational ruling need not create an unlimited repeatable ability.

[Improvisation, GM Core p. 12](https://2e.aonprd.com/Rules.aspx?ID=2486): use
established events, NPC personality and story context to determine reactions.
Actions meaningful to a player should receive an in-world response.

[Other Actions, Player Core p. 415](https://2e.aonprd.com/Rules.aspx?ID=2340):
the GM can assign an action cost and traits to an undefined action. Absence
from an action menu alone does not make an attempted action impossible.

## Shared resolution machinery

The stored [Playing the Game](playing-the-game.md) contains checks, degrees,
action sequencing, Interact, Seek, Sense Motive, Aid and exploration. The stored
[Difficulty Classes](difficulty-classes.md) contains simple and level-based DCs
and adjustments. Use the specified DC of an action or target where applicable;
15 is not a universal DC. A natural 20/1 adjusts the degree of an eligible
check; it does not create a missing capability or bypass prerequisites.

[Checks and Secret Checks, Player Core p. 400](https://2e.aonprd.com/Rules.aspx?ID=2278):
checks resolve uncertain outcomes. The GM can allow a player to roll a check
that would normally be secret. IIRPG's preference for player-operated dice can
therefore be preserved without discarding the distinction between knowledge
and world facts.

## Social actions are distinct

[Make an Impression, Player Core p. 239](https://2e.aonprd.com/Actions.aspx?ID=2392):
Diplomacy after at least a minute of conversation, normally against Will DC;
changes attitude for the interaction, rather than granting every requested act.

[Request, Player Core p. 239](https://2e.aonprd.com/Actions.aspx?ID=2393): a
one-action request to a friendly/helpful creature. Difficulty depends on the
request; success can include conditions. Some requests remain unacceptable.
Failure can produce refusal or an alternative, not an inert conversation loop.

[Coerce, Player Core p. 240](https://2e.aonprd.com/Actions.aspx?ID=2394): threats
over at least a minute, Intimidation against Will DC. Compliance, attitude,
retaliation and retry limits differ by degree. This is distinct from the
short combat fear effect of Demoralize.

[Sense Motive, Player Core p. 417](https://2e.aonprd.com/Actions.aspx?ID=2302):
Perception against the target's Deception DC or another appropriate DC. Ordinary
success assesses normality of behavior, while critical success reveals more
about intentions. Repeating normally requires a significant change in situation.
It is not a universal lie detector or unrestricted access to NPC knowledge.

The full social actions are stored in [Diplomacy](../skills/diplomacy.md) and
[Intimidation](../skills/intimidation.md); player-character attitudes and decisions
remain under their players' control.

## Objects and assistance

[Pick a Lock, Player Core p. 247](https://2e.aonprd.com/Actions.aspx?ID=2412):
trained Thievery, two actions, appropriate tools. Lock complexity determines
DC and possibly required successes. Critical success can advance two successes;
critical failure damages the toolkit. Improvised picks require a GM ruling and
are treated as shoddy tools. Full source: [Thievery](../skills/thievery.md).

[Force Open, Player Core p. 234](https://2e.aonprd.com/Actions.aspx?ID=2375):
Athletics against the obstacle; lacking a crowbar normally imposes an item
penalty. Opening by force differs from picking a lock, including damage and
jamming outcomes. Full source: [Athletics](../skills/athletics.md).

[Aid, Player Core p. 416](https://2e.aonprd.com/Actions.aspx?ID=2292): preparation
and a reaction to the ally's check, with a typical DC of 15. Degree and helper
proficiency determine the bonus/penalty. An agreement to help is not itself an
Aid roll, and the rule does not depend on a particular NPC, object or skill.

Opening a container, revealing contents, taking an item and damaging an object
are separate changes. See Interact in [Playing the Game](playing-the-game.md)
and object Hardness/HP in [Equipment](equipment.md).

## Provenance and boundaries

The new GM Core references above were checked on 2026-11-13 through Archives
of Nethys. Summaries are IIRPG adaptations of Licensed Material under ORC;
names from setting examples and source layout are omitted. Original stored
rule files and their hashes in `sources.json` are unchanged. This supplement
does not redefine them. Engine architecture proposed in
[general-action-rules.md](../../../docs/general-action-rules.md) is IIRPG design,
not a claim that Pathfinder prescribes a software protocol.
