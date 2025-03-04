import json
import logging
from typing import Dict, List, Tuple, NamedTuple, IO

from kloppy.domain import (
    BodyPart,
    BodyPartQualifier,
    CardType,
    CounterAttackQualifier,
    EventDataset,
    Ground,
    Metadata,
    Orientation,
    PassQualifier,
    PassResult,
    PassType,
    Period,
    Player,
    Point,
    Provider,
    Qualifier,
    SetPieceQualifier,
    SetPieceType,
    ShotResult,
    TakeOnResult,
    Team,
)
from kloppy.utils import performance_logging

from . import wyscout_tags
from ..deserializer import EventDataDeserializer
from .deserializer_v2 import WyscoutInputs


logger = logging.getLogger(__name__)


INVALID_PLAYER = "0"


def _parse_team(raw_events, wyId: str, ground: Ground) -> Team:
    team = Team(
        team_id=wyId,
        name=raw_events["teams"][wyId]["team"]["officialName"],
        ground=ground,
    )
    team.players = [
        Player(
            player_id=str(player["player"]["wyId"]),
            team=team,
            jersey_no=None,
            first_name=player["player"]["firstName"],
            last_name=player["player"]["lastName"],
        )
        for player in raw_events["players"][wyId]
    ]
    return team


def _has_tag(raw_event, tag_id) -> bool:
    for tag in raw_event["tags"]:
        if tag["id"] == tag_id:
            return True
    return False


def _generic_qualifiers(raw_event: Dict) -> List[Qualifier]:
    qualifiers: List[Qualifier] = []

    counter_attack_qualifier = CounterAttackQualifier(False)
    if raw_event["possession"]:
        if "counterattack" in raw_event["possession"]["types"]:
            counter_attack_qualifier = CounterAttackQualifier(True)
    qualifiers.append(counter_attack_qualifier)

    return qualifiers


def _parse_shot(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    if raw_event["shot"]["isGoal"] is True:
        result = ShotResult.GOAL
    elif raw_event["shot"]["onTarget"] is True:
        result = ShotResult.SAVED
    elif raw_event["shot"]["goalZone"] == "bc":
        result = ShotResult.BLOCKED
    else:
        result = ShotResult.OFF_TARGET

    if raw_event["shot"]["bodyPart"] == "head_or_other":
        qualifiers.append(BodyPartQualifier(value=BodyPart.HEAD))
    elif raw_event["shot"]["bodyPart"] == "left_foot":
        qualifiers.append(BodyPartQualifier(value=BodyPart.LEFT_FOOT))
    elif raw_event["shot"]["bodyPart"] == "right_foot":
        qualifiers.append(BodyPartQualifier(value=BodyPart.RIGHT_FOOT))

    return {
        "result": result,
        "result_coordinates": Point(
            x=float(0),
            y=float(0),
        ),
        "qualifiers": qualifiers,
    }


def _check_secondary_event_types(
    raw_event, secondary_event_types_values: List[str]
) -> bool:
    return any(
        secondary_event_types in secondary_event_types_values
        for secondary_event_types in raw_event["type"]["secondary"]
    )


def _pass_qualifiers(raw_event) -> List[Qualifier]:
    qualifiers = _generic_qualifiers(raw_event)

    if _check_secondary_event_types(raw_event, ["cross", "cross_blocked"]):
        qualifiers.append(PassQualifier(PassType.CROSS))
    elif _check_secondary_event_types(raw_event, ["hand_pass"]):
        qualifiers.append(PassQualifier(PassType.HAND_PASS))
    elif _check_secondary_event_types(raw_event, ["head_pass"]):
        qualifiers.append(PassQualifier(PassType.HEAD_PASS))
    elif _check_secondary_event_types(raw_event, ["smart_pass"]):
        qualifiers.append(PassQualifier(PassType.SMART_PASS))

    return qualifiers


def _parse_pass(raw_event: Dict, next_event: Dict, team: Team) -> Dict:
    pass_result = None
    receiver_player = None

    if raw_event["pass"]["accurate"] is True:
        pass_result = PassResult.COMPLETE
        receiver_player = team.get_player_by_id(
            raw_event["pass"]["recipient"]["id"]
        )
    elif raw_event["pass"]["accurate"] is False:
        pass_result = PassResult.INCOMPLETE

    if next_event:
        if next_event["type"]["primary"] == "offside":
            pass_result = PassResult.OFFSIDE
        if next_event["type"]["primary"] == "game_interruption":
            if next_event["type"]["secondary"] == "ball_out":
                pass_result = PassResult.OUT

    return {
        "result": pass_result,
        "qualifiers": _pass_qualifiers(raw_event),
        "receive_timestamp": None,
        "receiver_player": receiver_player,
        "receiver_coordinates": Point(
            x=float(raw_event["pass"]["endLocation"]["x"]),
            y=float(raw_event["pass"]["endLocation"]["y"]),
        )
        if len(raw_event["pass"]["endLocation"]) > 1
        else None,
    }


def _parse_foul(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    return {
        "result": None,
        "qualifiers": qualifiers,
    }


def _parse_card(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    card_type = None
    if _has_tag(raw_event, wyscout_tags.RED_CARD):
        card_type = CardType.RED
    elif _has_tag(raw_event, wyscout_tags.YELLOW_CARD):
        card_type = CardType.FIRST_YELLOW
    elif _has_tag(raw_event, wyscout_tags.SECOND_YELLOW_CARD):
        card_type = CardType.SECOND_YELLOW

    return {"result": None, "qualifiers": qualifiers, "card_type": card_type}


def _parse_recovery(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    return {
        "result": None,
        "qualifiers": qualifiers,
    }


def _parse_clearance(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    return {
        "result": None,
        "qualifiers": qualifiers,
    }


def _parse_ball_out(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    return {"result": None, "qualifiers": qualifiers}


def _parse_set_piece(raw_event: Dict, next_event: Dict, team: Team) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    result = {}

    # Pass set pieces
    if raw_event["type"]["primary"] == "goal_kick":
        qualifiers.append(SetPieceQualifier(SetPieceType.GOAL_KICK))
        result = _parse_pass(raw_event, next_event, team)
    elif raw_event["type"]["primary"] == "throw_in":
        qualifiers.append(SetPieceQualifier(SetPieceType.THROW_IN))
        qualifiers.append(PassQualifier(PassType.HAND_PASS))
        result = _parse_pass(raw_event, next_event, team)
    elif (
        raw_event["type"]["primary"] == "free_kick"
    ) and "free_kick_shot" not in raw_event["type"]["secondary"]:
        qualifiers.append(SetPieceQualifier(SetPieceType.FREE_KICK))
        result = _parse_pass(raw_event, next_event, team)
    elif (
        raw_event["type"]["primary"] == "corner"
    ) and "shot" not in raw_event["type"]["secondary"]:
        qualifiers.append(SetPieceQualifier(SetPieceType.CORNER_KICK))
        result = _parse_pass(raw_event, next_event, team)
    # Shot set pieces
    elif (
        raw_event["type"]["primary"] == "free_kick"
    ) and "free_kick_shot" in raw_event["type"]["secondary"]:
        qualifiers.append(SetPieceQualifier(SetPieceType.FREE_KICK))
        result = _parse_shot(raw_event)
    elif (raw_event["type"]["primary"] == "corner") and "shot" in raw_event[
        "type"
    ]["secondary"]:
        qualifiers.append(SetPieceQualifier(SetPieceType.CORNER_KICK))
        result = _parse_shot(raw_event)
    elif raw_event["type"]["primary"] == "penalty":
        qualifiers.append(SetPieceQualifier(SetPieceType.PENALTY))
        result = _parse_shot(raw_event)

    result["qualifiers"] = qualifiers
    return result


def _parse_takeon(raw_event: Dict) -> Dict:
    qualifiers = _generic_qualifiers(raw_event)
    result = None
    if "offensive_duel" in raw_event["type"]["secondary"]:
        if raw_event["groundDuel"]["keptPossession"]:
            result = TakeOnResult.COMPLETE
        else:
            result = TakeOnResult.INCOMPLETE
    elif "defensive_duel" in raw_event["type"]["secondary"]:
        if raw_event["groundDuel"]["recoveredPossession"]:
            result = TakeOnResult.COMPLETE
        else:
            result = TakeOnResult.INCOMPLETE
    elif "aerial_duel" in raw_event["type"]["secondary"]:
        if raw_event["aerialDuel"]["firstTouch"]:
            result = TakeOnResult.COMPLETE
        else:
            result = TakeOnResult.INCOMPLETE

    return {"result": result, "qualifiers": qualifiers}


def _players_to_dict(players: List[Player]):
    return {player.player_id: player for player in players}


class WyscoutDeserializerV3(EventDataDeserializer[WyscoutInputs]):
    @property
    def provider(self) -> Provider:
        return Provider.WYSCOUT

    def deserialize(self, inputs: WyscoutInputs) -> EventDataset:
        transformer = self.get_transformer(length=100, width=100)

        with performance_logging("load data", logger=logger):
            raw_events = json.load(inputs.event_data)
            for event in raw_events["events"]:
                if "id" not in event:
                    event["id"] = event["type"]["primary"]

        periods = []

        with performance_logging("parse data", logger=logger):
            home_team_id, away_team_id = raw_events["teams"].keys()
            home_team = _parse_team(raw_events, home_team_id, Ground.HOME)
            away_team = _parse_team(raw_events, away_team_id, Ground.AWAY)
            teams = {home_team_id: home_team, away_team_id: away_team}
            players = dict(
                [
                    (wyId, _players_to_dict(team.players))
                    for wyId, team in teams.items()
                ]
            )

            events = []

            for idx, raw_event in enumerate(raw_events["events"]):
                next_event = None
                if (idx + 1) < len(raw_events["events"]):
                    next_event = raw_events["events"][idx + 1]

                team_id = str(raw_event["team"]["id"])
                team = teams[team_id]
                player_id = str(raw_event["player"]["id"])
                period_id = int(raw_event["matchPeriod"].replace("H", ""))

                if len(periods) == 0 or periods[-1].id != period_id:
                    periods.append(
                        Period(
                            id=period_id,
                            start_timestamp=0,
                            end_timestamp=0,
                        )
                    )

                ball_owning_team = None
                if raw_event["possession"]:
                    ball_owning_team = teams[
                        str(raw_event["possession"]["team"]["id"])
                    ]

                generic_event_args = {
                    "event_id": raw_event["id"],
                    "raw_event": raw_event,
                    "coordinates": Point(
                        x=float(raw_event["location"]["x"]),
                        y=float(raw_event["location"]["y"]),
                    )
                    if raw_event["location"]
                    else None,
                    "team": team,
                    "player": players[team_id][player_id]
                    if player_id != INVALID_PLAYER
                    else None,
                    "ball_owning_team": ball_owning_team,
                    "ball_state": None,
                    "period": periods[-1],
                    "timestamp": float(
                        raw_event["second"] + raw_event["minute"] * 60
                    ),
                }

                primary_event_type = raw_event["type"]["primary"]
                secondary_event_types = raw_event["type"]["secondary"]
                if primary_event_type == "shot":
                    shot_event_args = _parse_shot(raw_event)
                    event = self.event_factory.build_shot(
                        **shot_event_args, **generic_event_args
                    )
                elif primary_event_type == "pass":
                    pass_event_args = _parse_pass(raw_event, next_event, team)
                    event = self.event_factory.build_pass(
                        **pass_event_args, **generic_event_args
                    )
                elif primary_event_type == "duel":
                    takeon_event_args = _parse_takeon(raw_event)
                    event = self.event_factory.build_take_on(
                        **takeon_event_args, **generic_event_args
                    )
                elif primary_event_type == "clearance":
                    clearance_event_args = _parse_clearance(raw_event)
                    event = self.event_factory.build_clearance(
                        **clearance_event_args, **generic_event_args
                    )
                elif (
                    (primary_event_type in ["throw_in", "goal_kick"])
                    or (
                        primary_event_type == "free_kick"
                        and "free_kick_shot" not in secondary_event_types
                    )
                    or (
                        primary_event_type == "corner"
                        and "shot" not in secondary_event_types
                    )
                ):
                    set_piece_event_args = _parse_set_piece(
                        raw_event, next_event, team
                    )
                    event = self.event_factory.build_pass(
                        **set_piece_event_args, **generic_event_args
                    )
                elif (
                    (primary_event_type == "penalty")
                    or (
                        primary_event_type == "free_kick"
                        and "free_kick_shot" in secondary_event_types
                    )
                    or (
                        primary_event_type == "corner"
                        and "shot" in secondary_event_types
                    )
                ):
                    set_piece_event_args = _parse_set_piece(
                        raw_event, next_event, team
                    )
                    event = self.event_factory.build_shot(
                        **set_piece_event_args, **generic_event_args
                    )

                else:
                    event = self.event_factory.build_generic(
                        result=None,
                        qualifiers=_generic_qualifiers(raw_event),
                        event_name=raw_event["type"]["primary"],
                        **generic_event_args
                    )

                if event and self.should_include_event(event):
                    events.append(transformer.transform_event(event))

        metadata = Metadata(
            teams=[home_team, away_team],
            periods=periods,
            pitch_dimensions=transformer.get_to_coordinate_system().pitch_dimensions,
            score=None,
            frame_rate=None,
            orientation=Orientation.BALL_OWNING_TEAM,
            flags=None,
            provider=Provider.WYSCOUT,
            coordinate_system=transformer.get_to_coordinate_system(),
        )

        return EventDataset(metadata=metadata, records=events)
