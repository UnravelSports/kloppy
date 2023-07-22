import json
import logging
from collections import defaultdict
from typing import Tuple, Dict, NamedTuple, Optional, Union, IO, Literal

from lxml import objectify

from kloppy.domain import (
    TrackingDataset,
    DatasetFlag,
    AttackingDirection,
    Frame,
    Point,
    Point3D,
    Team,
    BallState,
    Period,
    Provider,
    Orientation,
    attacking_direction_from_frame,
    Metadata,
    Ground,
    Player,
    build_coordinate_system,
    Provider,
    PlayerData,
)

from kloppy.utils import Readable, performance_logging

from ..deserializer import TrackingDataDeserializer
from ...event.sportec.deserializer import _sportec_metadata_from_xml_elm

logger = logging.getLogger(__name__)

PERIOD_ID_TO_GAME_SECTION = {
    1: "firstHalf",
    2: "secondHalf",
    3: "firstHalfExtra",
    4: "secondHalfExtra",
}


def _read_section_data(data_root, period: Period) -> dict:
    """
    Read all data for a single period from data_root.

    Output format:
    {
        10_000: {
            ('BALL', 'DFL-OBJ-0000XT'): {
                'x': 20.92,
                'y': 2.84,
                'z': 0.08,
                'speed': 4.91,
                'ballPossession': 2,
                'ballStatus': 1
            },
            ('DFL-CLU-000004', 'DFL-OBJ-002G3I'): {
                'x': 0.35,
                'y': -25.26,
                'speed': 0.00,
            },
            [....]
        },
        10_001: {
          ...
        }
    }
    """

    game_section = PERIOD_ID_TO_GAME_SECTION[period.id]
    frame_sets = data_root.findall(
        f"Positions/FrameSet[@GameSection='{game_section}']"
    )

    raw_frames = defaultdict(dict)
    for frame_set in frame_sets:
        key = (
            "ball"
            if frame_set.attrib["TeamId"] == "BALL"
            else frame_set.attrib["PersonId"]
        )
        for frame in frame_set.iterchildren("Frame"):
            attr = frame.attrib
            frame_id = int(attr["N"])

            object_data = {
                "x": float(attr["X"]),
                "y": float(attr["Y"]),
                "speed": float(attr["S"]),
            }
            if key == "ball":
                object_data.update(
                    {
                        "z": float(attr["Z"]),
                        "possession": int(attr["BallPossession"]),
                        "state": int(attr["BallStatus"]),
                    }
                )

            raw_frames[frame_id][key] = object_data

    return raw_frames


class SportecTrackingDataInputs(NamedTuple):
    meta_data: IO[bytes]
    raw_data: IO[bytes]


class SportecTrackingDataSerializer(TrackingDataDeserializer):
    @property
    def provider(self) -> Provider:
        return Provider.SPORTEC

    def __init__(
        self,
        limit: Optional[int] = None,
        sample_rate: Optional[float] = None,
        coordinate_system: Optional[Union[str, Provider]] = None,
        only_alive: Optional[bool] = True,
    ):
        super().__init__(limit, sample_rate, coordinate_system)
        self.only_alive = only_alive

    def deserialize(
        self, inputs: SportecTrackingDataInputs
    ) -> TrackingDataset:
        with performance_logging("load data", logger=logger):
            match_root = objectify.fromstring(inputs.meta_data.read())
            data_root = objectify.fromstring(inputs.raw_data.read())

        with performance_logging("parse metadata", logger=logger):
            sportec_metadata = _sportec_metadata_from_xml_elm(match_root)
            teams = home_team, away_team = sportec_metadata.teams
            periods = sportec_metadata.periods
            transformer = self.get_transformer(
                length=sportec_metadata.x_max, width=sportec_metadata.y_max
            )

        with performance_logging("parse raw data", logger=None):

            def _iter():
                player_map = {}
                for player in home_team.players:
                    player_map[player.player_id] = player
                for player in away_team.players:
                    player_map[player.player_id] = player

                sample = 1.0 / self.sample_rate

                for period in periods:
                    raw_frames = _read_section_data(data_root, period)

                    # Since python 3.6 dict keep insertion order
                    for i, (frame_id, frame_data) in enumerate(
                        raw_frames.items()
                    ):
                        if "ball" not in frame_data:
                            # Frames without ball data are corrupt.
                            print(frame_id, frame_data)
                            continue

                        ball_data = frame_data["ball"]
                        if self.only_alive and ball_data["state"] != 1:
                            continue

                        if i % sample == 0:
                            yield Frame(
                                frame_id=frame_id,
                                timestamp=(frame_id / sportec_metadata.fps)
                                - period.start_timestamp,
                                ball_owning_team=home_team
                                if ball_data["possession"] == 1
                                else away_team,
                                ball_state=BallState.ALIVE
                                if ball_data["state"] == 1
                                else BallState.DEAD,
                                period=period,
                                players_data={
                                    player_map[player_id]: PlayerData(
                                        coordinates=Point(
                                            x=raw_player_data["x"],
                                            y=raw_player_data["y"],
                                        ),
                                        speed=raw_player_data["speed"],
                                    )
                                    for player_id, raw_player_data in frame_data.items()
                                    if player_id != "ball"
                                },
                                other_data={},
                                ball_coordinates=Point3D(
                                    x=ball_data["x"],
                                    y=ball_data["y"],
                                    z=ball_data["z"],
                                ),
                            )

            frames = []
            for n, frame in enumerate(_iter()):
                frame = transformer.transform_frame(frame)

                frames.append(frame)

                if not frame.period.attacking_direction_set:
                    frame.period.set_attacking_direction(
                        attacking_direction=attacking_direction_from_frame(
                            frame
                        )
                    )

                if self.limit and n >= self.limit:
                    break

        print(len(frames))
        orientation = (
            Orientation.FIXED_HOME_AWAY
            if periods[0].attacking_direction == AttackingDirection.HOME_AWAY
            else Orientation.FIXED_AWAY_HOME
        )

        metadata = Metadata(
            teams=teams,
            periods=periods,
            pitch_dimensions=transformer.get_to_coordinate_system().pitch_dimensions,
            score=sportec_metadata.score,
            frame_rate=sportec_metadata.fps,
            orientation=orientation,
            provider=Provider.SPORTEC,
            flags=DatasetFlag.BALL_OWNING_TEAM | DatasetFlag.BALL_STATE,
            coordinate_system=transformer.get_to_coordinate_system(),
        )

        return TrackingDataset(
            records=[],
            metadata=metadata,
        )

    def serialize(self, dataset: TrackingDataset) -> Tuple[str, str]:
        raise NotImplementedError
