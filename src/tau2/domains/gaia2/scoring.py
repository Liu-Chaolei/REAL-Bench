"""Business-state assertions for Gaia2 tasks under tau2's reward composition."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from tau2.data_model.message import AssistantMessage
from tau2.domains.gaia2.location_rules import VOICE_LOCATION_RULES
from tau2.domains.gaia2.tools import Gaia2Tools
from tau2.environment.scenario import ScenarioRuntime, resolve_references

_ID = re.compile(r"^[0-9a-f]{32}$")
_EPHEMERAL = {
    "timestamp",
    "is_read",
    "time_read",
    "last_updated",
    "start_strftime",
    "end_strftime",
    "quotation_history",
    "api_call_limit",
    "order_date",
}
_TEXT = {"subject", "content", "description", "title"}
_LOCATION_TEXT = {"location", "start_location", "end_location"}

# These source tasks have a confirmed voice-input literal risk: the requested
# label comes from speech, so ASR/delegation may change case or punctuation while
# preserving the words. Variants inherit the rule through ``source_id``.
_VOICE_RELAXED_LITERAL_SOURCES = frozenset(
    {
        "0052_snsq4rd73gytoo0wr75bgd2nyzra07z1",
        "0058_0gz1go8jip8421i5ju1331o1xiavxc6q",
        "0066_6x8khlqdpmjxpwhjbm5ak8vstewvdj6e",
        "0068_gbelbkkh09hbtozeyzl9fo46xjjy34wt",
        "0070_bnph7yeo7g84q838ck466s6ekp9evure",
        "0072_u4g0mv9o9jr41qkuldutae45arx3s1my",
        "0079_u0u8jye1kgslgrxicfe2tv85jb5cqk4g",
        "0080_8st555dtg372vfx0440tcdgt6x5wsoot",
        "0082_ydvnfzlpi1maeeoui4pljydk6xvkq9xm",
        "0092_jee41ekazvchzdmlkejdxjtulcqtalen",
        "0093_zdhuiup4pf7ohlmp4gw7kbug2oh0y2jq",
        "0095_wyy7yrols46xw6d8b58bq4652uf9urnd",
        "0098_kv0q6p0s0rtklxfz4udc4w8v1ae4qibk",
        "0099_dys7qiasymc6tsiomethj9v7ofhlksfb",
        "0105_8lf1buk2eflaicr58ite7st8v93dhr4y",
        "0112_vkqflpo2xi3soprbqkko12hnqrn5an7z",
        "0117_21kzyhlzscui6zvrqouyc1n2xuyhkbal",
        "0120_3vsfj9zev9p0ildevi5f2xho5m8bsuyj",
        "0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o",
        "0147_knh0fssbzcq8a1lytctvobpulktdavgv",
        "0170_pqaibivaqu8g0dhcpd2fu2dpbq287iyh",
        "0184_2t3mipvaxiauqgo88o5l3bt9fryft175",
        "0187_wk3hw6dtnbrvzs94r1cjqmkwogkjziv6",
        "0193_3spn1z5u8lotfe4gdqelx7yx3k8d25xu",
        "0197_da4x890dllfqmlhxx79l1rxypwc8op76",
        "0205_4601iajlmbvrb8vd8zgmvgiomm317tsw",
        "0214_eht1on2zsfnwid1zfnadsqwx1zmpsvzq",
        "0218_tawf8t6kl1mq1y716wmi8fk8oi200q1l",
        "0321_ix3k7msla5r85tkfecwtwptv7x4jbkvr",
        "0325_pixli5p6bhxqhjdu02nggaum92ub8hla",
        "0326_cua96jjihfu9cuznvuu8dimpkuxb690t",
        "0333_8tx19jlc8y203vmvtu6dxj3t64kue5r2",
        "0334_w4362b7r0l35e4yc9is7qyhn96g471yb",
        "0335_nfjg63mx1wtpcdxz7i229twcs50yx26s",
        "0337_6drzncivla41w84ktsxq3wyzv6xaa8fh",
        "0356_inml2dkdrq3za2yqge84xexyflkzx1ay",
        "0359_gdeiiu80uh6kyp95s1xtf7v02r5vub8z",
        "0374_dsbw566w83tsj8gahist6yifo2x2pdwf",
        "0383_c3eoypm9irwbeg9t4tdgk7w0gxeexu15",
        "0452_wy5m8xi2s8h377krn5o5r616w1iv36km",
        "0454_51zeute4refgj8p4j3o4fkjozr1fz3tz",
        "0457_zxo5lmj0fw7vloj0hvnnoo2ofui6qnoq",
        "0463_ama85inj9b2ej1fozxte0xv4ib1ejidk",
        "0466_4tmw3ae17khbui8x4yjj3gi9asl0guh2",
        "0468_zh5zoync3yepme11k112jsebclkwcf6c",
        "0474_nvrutne256q0mv5mwcdytmpozvpjjy8y",
        "0475_mss3ybmgsiohog377bh8st4hw83i37ji",
        "0479_grlxsenm02c1hff3oqboxku2szsvuvn4",
        "0481_qihzpfzibjk90gpmd1xswmksyd2j87fg",
        "0482_lgssisqni51hw6mqy98wdmosvdw5otwf",
        "0485_a0ohub8xs1haggirfeqj222byuj003m8",
        "0490_khnzq2qhgu6tjw8aki3jhx9vkydax3z7",
        "0494_n4wtodzfdv1libenohismv4r2gvzbctk",
        "0506_5gqyrsvi2qeh63muwkq5o6jetw717lnb",
        "0508_5cdue25isi1x33irzh4ctpbw4gf8verj",
        "0509_dwosravr2wwaxvjqpelc7bcvh6xrm398",
        "0511_798bbbiqb42ufq67w607yig61if55vh8",
        "0516_1xv9btt2d876mjh70tspq59bb1bcnwbh",
        "0530_nvz2d2e5rjnkqre1x2e8j2mn8rae9xj1",
        "0531_29mm6aztw8ah4z0mvinz0vltfuogbdba",
        "0540_c7qrfnor6pea230wfdawbm2humo6clj1",
        "0544_wjcnkvefzwp4xvw547eoxgx6jqy5nle7",
        "0546_thyowfbi0uxq8niuyvg8jq519cqrvc06",
        "0564_ypwfbc08vasotmlepqmhr7wzbk53ntqv",
        "0568_0pk54gkq73erpxm7s6g80wf0ecs6otd8",
        "0573_mug2chs6287zgcfq667kl4syngytosq9",
        "0574_3x70omb0stj3hf0lv7hw5a3rnd6xhyzi",
        "0579_6n668fwqc5g3rgj1nj9stog9dze1vono",
        "0582_190kzw8rs1c4qoc3ui7bixy1mk5n33r2",
        "0583_ns8obeufgq2qy0o0vtgvhvykadoau4po",
        "0586_fj9jybhselpm9g6shui65hmd0wg6gcvy",
        "0588_pvn1buv8592zl2olqn8e0brc8r1sxlsv",
        "0591_ki3cgrlql0qph5k418vxpvtxzttm72f1",
        "0593_1faw6iohw5i810br3lv3ptb9a4xebg7d",
        "0599_k85r3osbcdy1xz12mdupt739oev45qe4",
        "0601_mz4dh4tml93gims9dxwrcz7i16t0p5ud",
        "0602_sy6r944liwulln91ravyrcctdirinr0d",
        "0603_pofr8ljj8zqoeamwjnrvlu23397rqasi",
        "0604_ifl4g3s8d6urq1fwtet26rl98sqrsshp",
        "0605_dydb3p6l38hpusrecwguna7puzvem294",
        "0607_mo3yh3fqpxo6jdsrdabi02k9p19vyvzx",
        "0608_f9dnksx1xvs5mvxg8qcax32clsp3b3bo",
        "0613_yizeir0okkaixm3wd7s6o0j9u94uy4e1",
        "0615_jm4ojcw236ntst78utcy7lbavn40o141",
        "0616_ewvwwn0b9kt8urb5oa6sh2ui5w1xi6tt",
        "0621_uuah385lesh5u44896fr450ntypjb8ge",
        "0622_cftp478g1x3kbeidx1yu0yjsrippi8b8",
        "0624_u8fx1mox9nkpxmy0tlmnibgjmklrxknr",
        "0625_0ks0aepjjmzjfqpzdm570w2nnwo6pg15",
        "0630_dtpdnzfw3exlo58hz9vgo20jvpr7fikk",
        "0637_wz3bcrlckr0fmca6xlgc3grjipbvgr0p",
        "0643_750ea4udiytzv2211crwkewkhydnq5lx",
        "0647_aakouu8f2upgc0zggnclrosoqdhc8gup",
        "0658_mx62nptyc2l15aoa2nbmztuqfljq14uf",
        "0660_l3y0egytbn6wqk388pgjllndgy1eeckw",
        "0663_c4rhds3gn4hecrdzan4k442j69gsgzsl",
        "0666_2dsmofbug7ytmczfxa1a7y8xch0suuzh",
        "0669_495klcxaz8n9goiax1lueczgn1p5w2qs",
        "0670_hfkpojfb4myigbc32a5xl6zcv249alao",
        "0673_ogqcmwevz8lm7rmnaxh65h6wptk8gvhe",
        "0676_6xd4atv0d4vcqppx40utwoskts52vzvb",
        "0680_wmve2jrtqqqxwe54zykox0z7ipt59rgg",
        "0684_czfzxqol5wzfbed4rk9l2zcys6i2cbjt",
        "0690_1b9hg1u36ugt0cnr2ksoeabyob36ofbx",
        "0696_b39cceovqwq08978gues62vz510h1oko",
        "0697_sn451pnnoz19j0o0fylr8onyo828f3tn",
        "0698_pkyb3pflukov2hhnolhiv5cs9rfkf5tr",
        "0703_e6nz1ppwdudnjel0ojtznkulnfeqewot",
        "0707_i706q09ixk7eh1bghozdtp5b7mz9jb7n",
        "0710_e2cjolirnhypj7tgr44nl3woxkfrgtu2",
        "0714_d72zg6tmwxz71o37swrc8eef6vnjun4a",
        "0729_lxgewxtbh4vhgalf0gxin1jsyt3gcoh4",
        "0735_sbonsywxnoryge3wlcev7sxiqje8uyti",
        "0738_n4xwmqgsl7dvjsghds5w0d5w3oocex4a",
        "0742_dsyw8e3mte0tf4e9uo7qiwvhy31a47y5",
        "0751_hg05wxw1rn37kk3f8t1v1mwk7el8a7nz",
        "0752_b5jn0068aw58wxmfwdxp4jl3hly1yrxd",
        "0757_re3sq3a9uj431zkwmeqydxq96goyrlqo",
        "0761_uyy5f6lri562ysemdm8io8gxn8r9kpku",
        "0768_21zn1994p6oj0bvqpasy1ttpxq9rv0ua",
        "0769_o9y8sx93prlp5f4zwoxm45ue6smjzm58",
        "0774_l8z1hvo01snf4z0hro725g4fwbq49gsc",
        "0775_2i8lurt0fk61enyvoebqkhspuis8v8x7",
        "0778_daz1u5crswv63bja8j5sbs0i5676egtq",
        "0781_5zicvzatjwmvh9vk23wpmgsyv3wpm04t",
        "0788_pxk0p7ls93kumrrwrvedne6jh3ffpacx",
        "0790_cljd6invhqwlhvau5ao73d3ger6j9oua",
        "0797_2gfnv6qrc1ekxwg8bksxmc17e7bfidwr",
        "0799_2ss7fqbcoko8voc1def0vh6ernx6paiy",
        "0800_mtpdtggicbmuvw8b8hueo01a08w7blrn",
        "0809_9co8zythg5hwj4oq46wpxhn3jfjaffjk",
        "0812_qlzb47am926hwepov29ts6qei6y0clzq",
        "0822_1slhgnka10l1kjii7nqgamgznxjo4loh",
        "0823_rwzr2hwaiw4ei864km2rhwdwgdv52zn2",
        "0824_46kas9zw1xdqycjnsmz6y64rficzzbpu",
        "0828_pif6bas6i9cescsq7v5tq3zppmag47b9",
        "0831_xy05tehvc5zeadqnxzq5g61vqqar6z2i",
        "0833_fr1ha583m0e86zdtmoen0yu4onbk5bvl",
        "0835_3fvz2bnzb3w6avwsl92pkpk8uz28hais",
        "0836_fmb7fdp4py1yzn1gziwg9fh7zs1sonu7",
        "0840_dsv5vi74crkfach79434gqawcq614wep",
        "0844_ptf9fhdhy96bb008hlv4zn52sg8v5fva",
        "0853_242ovkbi72tuxkmovexnua7fpsd840vj",
        "0854_2am9nvz07nhuw21mpi9itzazouq7xb23",
        "0856_i32itsbia7kjdqpougc54c7xik50fpti",
        "0862_b078fyjbqz9gdr1oere8025xpu9wg5sc",
        "0880_1aeo5r6r2dfv8ix1viup0311nznv3ke8",
        "0882_ny7zkoyboiurvvuzctmyuavdc9icpk91",
        "0883_20h78i8ptuul6yuhk6cke8lzvmp929c2",
        "0885_xprway7wdx5vwlrh2upaefyi7qkmzy6b",
        "0888_51l8dcuw8jy68uqr68f725b0mk479ar2",
        "0889_9q80g77sgsgo9mtf8vh3i2pzjm7gwjdk",
        "0898_h47f9xaz8othydwkcxky3us1ucsllkl0",
        "0904_gocwz8pnoivooay3vsx1i8aoe729yep5",
        "0907_o8lhhq00pjqngpvp00cu0ireperv6ssu",
        "0914_y70s9nda9x1kae513hc6vs39oi568yak",
        "0921_92cwy9d3m4rlm3x8ifmm7fkey975fqkq",
        "0927_ox4f2a4q0zjrgo92ach8m88gdyzxkavq",
        "0929_gnj6ea1qobi8dn9joe3rf3p8zkj3fnge",
        "0930_bs9rikfk9v9px9kdy0b7ztru657zreek",
        "0932_5jfayayraae5kkh471epw5qhdjb6c5sl",
        "0936_udu5sabfyk4f149l3yoxqvfk728ouzph",
        "0937_nrl2qrvpwiq9mby9qwcajt1yafjeygbi",
        "0938_c0z8k5bduwb35otlxlk83v2ax8h9ruep",
        "0942_ittipbqa3jcxj9dgt8loojtlh354c1i2",
        "0951_o0wr8y4qyelaw1yz473ci1u4nitx35jw",
        "0954_g1n18e1zvd1yz88nueb68i8qoxg5wb5m",
        "0956_8z9q8qtsjkhyodepu3qp522i9qnte9r1",
        "0959_c7btgxh6taik6kfo9fczpvhbxy5fjx0y",
        "0964_vnkaekwpyg2ovh4z3lrmqy5pumnyxn1n",
        "0965_n4ra84f060a7td64qkvzfvnbh64rum0u",
        "0966_hjk2wdizvpqaa5jvu0wnroulc26mkz46",
        "0968_awm15kgqgj7s5azcpbf3kygytooq5ucv",
        "0970_4uoqxevy5cgtpnc1xce1n89g5gjbczh1",
        "0982_nqa42peg3anfzs7v0mcna20scf6r5dn0",
        "0987_hcvpyfaca1f5ejmd7nqk65dn1xwfpaf7",
        "0996_vsp65a1ozwhggqpb4s0754fts1l4r0xx",
    }
)


def reference_execution(snapshot: list[dict], spec, assets: Path):
    """Derive a target by executing the source operations with a virtual clock.

    This is data preparation/reward evaluation, never an alternate agent runner.
    No model, user proxy, ARE Environment or ARE judge is constructed.
    """
    target = Gaia2Tools(snapshot, assets=assets)
    runtime = ScenarioRuntime(spec, target.execute)
    target.runtime = runtime
    for app in target.apps.values():
        app.register_time_manager(runtime)
    pending = list(spec.events)
    try:
        while pending:
            ready = [
                e
                for e in pending
                if all(p in runtime.completed for p in e.dependencies)
            ]
            if not ready:
                raise ValueError("Reference operations have unresolved dependencies")
            ready.sort(key=runtime.event_priority)
            event = ready[0]
            due = (
                max((runtime.completed[p] for p in event.dependencies), default=0)
                + event.delay_seconds
            )
            runtime._manual = max(runtime.elapsed, due)
            arguments = resolve_references(event.arguments, runtime.results)
            if (
                event.kind == "user"
                or event.tool == "AgentUserInterface__send_message_to_user"
            ):
                result = None
            else:
                result = target.execute(event.tool, arguments)
            runtime.results[event.id] = result
            runtime.completed[event.id] = runtime.elapsed
            pending.remove(event)
        target.refresh_db(include_files=True)
        return copy.deepcopy(target.db.apps), dict(runtime.results)
    finally:
        target.close()


def _initial_identifiers(value) -> set[str]:
    if isinstance(value, dict):
        result = {str(k) for k in value if _ID.fullmatch(str(k))}
        for child in value.values():
            result.update(_initial_identifiers(child))
        return result
    if isinstance(value, list):
        result = set()
        for child in value:
            result.update(_initial_identifiers(child))
        return result
    return {value} if isinstance(value, str) and _ID.fullmatch(value) else set()


def business_state(value, initial_ids: set[str]):
    """Represent generated identities as variables, retaining all foreign keys."""
    if isinstance(value, dict):
        pairs = []
        generated_keys = False
        for key, child in value.items():
            if key in _EPHEMERAL:
                continue
            key = str(key)
            if _ID.fullmatch(key) and key not in initial_ids:
                key = {"@generated_id": key}
                generated_keys = True
            pairs.append([key, business_state(child, initial_ids)])
        return {"@entities": pairs} if generated_keys else dict(pairs)
    if isinstance(value, list):
        return [business_state(child, initial_ids) for child in value]
    if isinstance(value, str) and _ID.fullmatch(value) and value not in initial_ids:
        return {"@generated_id": value}
    return value


def _equivalent(
    actual,
    expected,
    *,
    field="",
    scoring=None,
    differences=None,
    literal_texts=None,
    relaxed_literal_texts=None,
    location_rules=None,
    state_bindings=None,
):
    """Match entity graphs bijectively, backtracking across unordered collections.

    Semantic text checks never determine identity by themselves. One actual
    identifier must correspond to exactly one target identifier everywhere,
    including references from other applications. Backtracking permits harmless
    creation-order changes without losing these relationships.
    """
    import math

    from tau2.config import DEFAULT_SCENARIO_STATE_MATCH_LIMIT

    literals = literal_texts or set()
    relaxed_literals = relaxed_literal_texts or set()
    location_rules = location_rules or {}
    cache = {}
    visits = 0

    def marker(value):
        return isinstance(value, dict) and set(value) == {"@generated_id"}

    def bind(left, right, mapping):
        if left in mapping:
            return mapping if mapping[left] == right else None
        if right in mapping.values():
            return None
        return {**mapping, left: right}

    def equal_identifiers(value, mapping):
        if marker(value):
            return bind(value["@generated_id"], value["@generated_id"], mapping)
        children = (
            value.values()
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else []
        )
        for child in children:
            mapping = equal_identifiers(child, mapping)
            if mapping is None:
                break
        return mapping

    def has_identifiers(value):
        if marker(value):
            return True
        children = (
            value.values()
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else []
        )
        return any(has_identifiers(child) for child in children)

    def solve(left, right, key, mapping, checks):
        nonlocal visits
        visits += 1
        if visits > DEFAULT_SCENARIO_STATE_MATCH_LIMIT:
            raise RuntimeError(
                "Business-state comparison exceeded its declared search limit"
            )
        if key in _TEXT and (
            (left is None and right == "") or (right is None and left == "")
        ):
            yield mapping, checks
            return
        if marker(left) or marker(right):
            if marker(left) and marker(right):
                updated = bind(left["@generated_id"], right["@generated_id"], mapping)
                if updated is not None:
                    yield updated, checks
            return
        if left == right:
            updated = equal_identifiers(left, mapping)
            if updated is not None:
                yield updated, checks
            if not has_identifiers(left):
                return
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return
            if set(right) == {"@entities"}:
                # A single generated key must not turn hundreds of original IDs
                # into a permutation problem. Keep ID/value pairs atomic.
                def partition(pairs):
                    fixed, generated = {}, []
                    for identity, value in pairs:
                        if marker(identity):
                            generated.append({"identity": identity, "entity": value})
                        else:
                            fixed[identity] = value
                    return fixed, generated

                left_fixed, left_new = partition(left["@entities"])
                right_fixed, right_new = partition(right["@entities"])
                if left_fixed.keys() != right_fixed.keys() or len(left_new) != len(
                    right_new
                ):
                    return
                for updated, rows in solve(
                    left_fixed, right_fixed, key, mapping, checks
                ):
                    yield from solve(left_new, right_new, key, updated, rows)
                return
            # Cheap structural/identity mismatches precede semantic text checks.
            keys = sorted(
                right,
                key=lambda name: (name in _TEXT, isinstance(right[name], (dict, list))),
            )
            failed_fields = set()

            def fields(index, current, selected):
                state = (index, tuple(sorted(current.items())))
                if state in failed_fields:
                    return
                if index == len(keys):
                    yield current, selected
                    return
                name = keys[index]
                matched = False
                for updated, rows in solve(
                    left[name], right[name], name, current, selected
                ):
                    for outcome in fields(index + 1, updated, rows):
                        matched = True
                        yield outcome
                if not matched:
                    failed_fields.add(state)

            yield from fields(0, mapping, checks)
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return
            failed_items = set()

            def items(index, available, current, selected):
                state = (index, available, tuple(sorted(current.items())))
                if state in failed_items:
                    return
                if index == len(right):
                    yield current, selected
                    return
                candidates = sorted(available, key=lambda i: left[i] != right[index])
                matched = False
                for candidate in candidates:
                    for updated, rows in solve(
                        left[candidate], right[index], key, current, selected
                    ):
                        for outcome in items(
                            index + 1,
                            tuple(i for i in available if i != candidate),
                            updated,
                            rows,
                        ):
                            matched = True
                            yield outcome
                if not matched:
                    failed_items.add(state)

            yield from items(0, tuple(range(len(left))), mapping, checks)
            return
        if (
            isinstance(left, (int, float))
            and isinstance(right, (int, float))
            and not isinstance(left, bool)
            and not isinstance(right, bool)
        ):
            if math.isclose(left, right, rel_tol=0, abs_tol=1e-9):
                yield mapping, checks
            return
        if isinstance(left, str) and isinstance(right, str) and key in _TEXT:
            if right in literals:
                if right in relaxed_literals and _same_words(left, right):
                    yield mapping, checks
                return
            if " ".join(left.split()).casefold() == " ".join(right.split()).casefold():
                yield mapping, checks
                return
            if scoring is not None and scoring.scorer.value == "semantic":
                from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

                pair = (left, right)
                if pair not in cache:
                    result = CommunicateEvaluator.evaluate_communicate_info(
                        [AssistantMessage(role="assistant", content=left)],
                        [right],
                        scoring=scoring,
                    )[0]
                    cache[pair] = {
                        "field": key,
                        "expected": right,
                        "actual": left,
                        "met": result.met,
                        "justification": result.justification,
                    }
                row = cache[pair]
                if row["met"]:
                    yield mapping, checks + [row]

        if isinstance(left, str) and isinstance(right, str) and key in _LOCATION_TEXT:
            rule = location_rules.get(key, {}).get(right)
            if rule is None:
                return
            if rule["mode"] == "mixed" and not all(
                fragment in left for fragment in rule["environment_fragments"]
            ):
                return
            if _same_words(left, right):
                yield mapping, checks
            return

    solution = next(solve(actual, expected, field, {}, []), None)
    if differences is not None:
        differences.extend(solution[1] if solution is not None else cache.values())
    if solution is not None and state_bindings is not None:
        state_bindings.update(solution[0])
    return solution is not None


def check_goal(tools: Gaia2Tools) -> bool:
    """Compare actual business outcomes with the frozen task's target outcomes."""
    spec = tools.runtime.spec
    target, reference_values = reference_execution(
        tools.source_snapshot, spec, tools.assets
    )
    identifiers = _initial_identifiers(tools.source_snapshot)
    tools.refresh_db(include_files=True)
    actual = business_state(
        _project_outcomes(tools.db.apps, tools.source_snapshot, spec), identifiers
    )
    expected = business_state(
        _project_outcomes(target, tools.source_snapshot, spec), identifiers
    )
    details = []
    state_bindings = {}
    literal_labels = _literal_labels(spec)
    met = _equivalent(
        actual,
        expected,
        scoring=tools.semantic_scoring,
        differences=details,
        literal_texts=literal_labels,
        relaxed_literal_texts=(
            literal_labels
            if spec.source_id in _VOICE_RELAXED_LITERAL_SOURCES
            else set()
        ),
        location_rules=VOICE_LOCATION_RULES.get(spec.source_id, {}),
        state_bindings=state_bindings,
    )
    inverse = {expected: actual for actual, expected in state_bindings.items()}

    def actual_result(value):
        if isinstance(value, dict):
            return {key: actual_result(child) for key, child in value.items()}
        if isinstance(value, list):
            return [actual_result(child) for child in value]
        if isinstance(value, str):
            for expected, actual in inverse.items():
                value = value.replace(expected, actual)
        return value

    tools.reference_bindings = (
        {key: actual_result(value) for key, value in reference_values.items()}
        if met
        else {}
    )
    tools.goal_diagnostics = {
        "version": "gaia2-tau-v1",
        "state_match": met,
        "semantic_checks": details,
    }
    return met


def _project_outcomes(state, snapshot, spec):
    """Grade cab bookings by requested effects, rather than random quote draws."""
    state = copy.deepcopy(state)
    original = _initial_identifiers(snapshot)
    explicit_times = any(
        e.tool == "Cabs__order_ride" and e.arguments.get("ride_time")
        for e in spec.events
    )
    from tau2.domains.gaia2.tools import location_aliases, normalize_arguments

    aliases = location_aliases(snapshot)
    for ride in state.get("Cabs", {}).get("ride_history", []):
        if ride.get("ride_id") in original:
            continue
        for key in ("price", "duration", "delay", "delay_history", "distance_km"):
            ride.pop(key, None)
        if not explicit_times:
            ride.pop("time_stamp", None)
        for key in ("start_location", "end_location"):
            if key in ride:
                ride[key] = normalize_arguments(
                    "Cabs__order_ride", {key: ride[key]}, aliases
                )[key]
    return state


def _literal_labels(spec):
    """Keep explicitly quoted event titles and email subjects exact."""
    pattern = re.compile(
        r"\b(?:title|titled|name|named|called|subject)(?:\s+\w+){0,5}?\s*[, :\-]?\s*(?:[\"“]([^\"”]+)[\"”]|['‘]([^'’]+)['’])",
        re.IGNORECASE,
    )
    text = " ".join(
        str(e.arguments.get("content", "")) for e in spec.events if e.kind == "user"
    )
    return {match.group(1) or match.group(2) for match in pattern.finditer(text)}


def _same_words(left: str, right: str) -> bool:
    """Compare a voice literal by words, ignoring case and punctuation.

    This is intentionally narrower than semantic matching: token order and
    multiplicity remain significant, while ASR-friendly punctuation, casing,
    and separators may differ.
    """
    word_pattern = r"[^\W_]+"
    return [
        token.casefold() for token in re.findall(word_pattern, left, re.UNICODE)
    ] == [token.casefold() for token in re.findall(word_pattern, right, re.UNICODE)]
