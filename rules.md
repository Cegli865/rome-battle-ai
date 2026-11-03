# Game rules for the AI general

Facts about the game and hard rules of this mod: always true, the general must plan with them. Tactical advice
is in [strategy.md](strategy.md). Both files are re-read before every battle (no server restart needed). One
rule = one bullet; only lines starting with "- " are sent to the model. The executor takes no decisions of its
own: these rules are followed only as far as the general (the LLM) follows them. last_resort=true marks a
group that breaks one.

- The general (the unit marked GENERAL) matters more than any other unit: his death breaks the morale of the whole army. His RALLY ability steadies only the units near him.
- Wall towers shoot at every enemy unit within roughly 150 m of them (T.. in the TOWN GRAPH): units standing or marching near the walls, and artillery brought close to them, are shot to pieces.
- In a siege battle the objective is always the town square. The defender defends the square; the attacker tries to take it. If the attacker holds the square, the town falls and the battle is lost, no matter how the rest of the fight goes.
- Siege equipment (rams, ladders, siege towers) is used by the game's own AI: the units carrying it are not under your command (marked COMMANDED BY THE GAME'S AI) until the equipment is used, dropped or destroyed - with ladders or a siege tower, once the men are up on the wall. In siege battles the game's AI also fires your artillery until its ammunition runs out; the artillery advice applies to it only once it is yours.
- Units cannot pass a CLOSED gate (see GATES in each plan): sent through it they only crowd in front of it under tower fire. A gate can be used once it is broken, or opened from the gatehouse (G..w in the TOWN GRAPH) by your units that hold it; rams may of course go to a closed gate to batter it.
- A frontal cavalry charge on spear units (spearmen, hoplites, pikemen - "spear" in UNIT TYPES), and above all on a unit in phalanx or schiltrom formation (marked PHALANX), is almost certainly lost. Units with swords, axes and other non-spear weapons can be charged from the front.
- Artillery flaming ammunition (use_ability on, if the unit has it) kills more men per hit; normal ammunition (use_ability off) is more accurate.
- Artillery (catapults, onagers, ballistas) is inaccurate and its shots hit everyone where they land, your own men too.
- An artillery unit whose machines are gone (the unit list shows "0 machines - ALL DESTROYED") is harmless: there is no need to kill its crew.
- Elephants and chariots can run amok and then trample their own side. Shooting at elephants, especially with flaming arrows or flaming ammunition, can quickly drive them into such a frenzy.
- A unit in phalanx, testudo or schiltrom formation cannot run.
- Fatigue: running tires units, and tired or exhausted units fight worse and break and flee sooner. The "tired" / "exhausted" marks on your units show it; units recover when they rest.
- task=withdraw takes a unit off the battlefield only if the briefing says WITHDRAWAL IS POSSIBLE. A unit with keep_range_m backs off from every enemy that comes near, for as long as it is set.
- Units that "hide in forest" / "hide well in forest" / "hide in long grass" / "hide anywhere" (see UNIT TYPES) are invisible to the enemy while they stand still in that terrain (forest on the TERRAIN MAP). A hidden unit that moves or shoots gives itself away.
- Give every unit a task in every plan. Units left out of the plan stand still and do nothing.
- Hard rule for the general: whenever any enemy unit is within 200 m of the general, every order you give the general's unit has run=true - no exceptions, even for a short move inside your own formation. A walking general is caught by enemy cavalry.
