import logging
from collections import defaultdict
from datetime import timedelta, timezone
from dateutil.parser import parse
from typing import NamedTuple, IO, Optional, Union, Dict
import numpy as np
import json
import bz2
import io
import csv
from ast import literal_eval

from kloppy.domain import (
    attacking_direction_from_frame,
    AttackingDirection,
    DatasetFlag,
    Frame,
    Ground,
    Metadata,
    Orientation,
    Period,
    Player,
    PlayerData,
    Point,
    Point3D,
    PositionType,
    Provider,
    Team,
    TrackingDataset,
)

from kloppy.infra.serializers.tracking.deserializer import (
    TrackingDataDeserializer,
)

from kloppy.utils import performance_logging
from kloppy.io import FileLike

logger = logging.getLogger(__name__)

# frame_rate = 10

position_types_mapping: Dict[str, PositionType] = {
    "CB": PositionType.CenterBack,  # Provider: CB
    "LCB": PositionType.LeftCenterBack,  # Provider: LCB
    "RCB": PositionType.RightCenterBack,  # Provider: RCB
    "LB": PositionType.LeftBack,  # Provider: LB
    "RB": PositionType.RightBack,  # Provider: RB
    "DM": PositionType.DefensiveMidfield,  # Provider: DM
    "CM": PositionType.CenterMidfield,  # Provider: CM
    "LW": PositionType.LeftWing,  # Provider: LW
    "RW": PositionType.RightWing,  # Provider: RW
    "D": PositionType.CenterBack,  # Provider: D (mapped to CenterBack)
    "CF": PositionType.Striker,  # Provider: CF
    "M": PositionType.CenterMidfield,  # Provider: M (mapped to CenterMidfield),
    "GK": PositionType.Goalkeeper,  # Provider: GK
    "F": PositionType.Striker,  # Provider: CF
}


class PFF_TrackingInputs(NamedTuple):
    meta_data: IO[bytes]
    roster_meta_data: IO[bytes]
    raw_data: FileLike


class PFF_TrackingDeserializer(TrackingDataDeserializer[PFF_TrackingInputs]):
    def __init__(
        self,
        limit: Optional[int] = None,
        sample_rate: Optional[float] = None,
        coordinate_system: Optional[Union[str, Provider]] = None,
        include_empty_frames: Optional[bool] = False,
    ):
        super().__init__(limit, sample_rate, coordinate_system)
        self._ball_owning_team = None
        self.include_empty_frames = include_empty_frames

    @property
    def provider(self) -> Provider:
        return Provider.PFF

    @classmethod
    def _get_frame_data(
        cls,
        teams,
        players,
        periods,
        ball_owning_team,
        frame,
    ):
        '''Gets a Frame'''
        
        # Get Frame information
        frame_period = frame["period"]
        frame_id = frame["frameNum"]
        frame_timestamp = timedelta(seconds=frame["periodGameClockTime"])

        # for k, v in frame.items():
        #     print(k, v)

        # print(frame)

        # print("-----")
        
        # Ball coordinates
        ball_smoothed = frame.get("ballsSmoothed")
        if ball_smoothed:
            ball_x = ball_smoothed.get("x")
            ball_y = ball_smoothed.get("y")
            ball_z = ball_smoothed.get("z")
        
            ball_coordinates = Point3D(
                x=float(ball_x) if ball_x is not None else None,
                y=float(ball_y) if ball_y is not None else None,
                z=float(ball_z) if ball_z is not None else None,
            )
            
        else:
            ball_coordinates = Point3D(x=None, y=None, z=None)

        # Player coordinates
        players_data = {}

        if frame["homePlayersSmoothed"] is not None:
            for home_player in frame["homePlayersSmoothed"]:
                for p_id, player in players["HOME"].items():
                    if player.jersey_no == str(home_player["jerseyNum"]):
                        # player_id = p_id
                        break

                home_player_x = home_player.get("x") if home_player else None
                home_player_y = home_player.get("y") if home_player else None

                player_data = PlayerData(
                    coordinates=Point(home_player_x, home_player_y)
                )
                players_data[player] = player_data

        if frame["awayPlayersSmoothed"] is not None:
            for away_player in frame["awayPlayersSmoothed"]:
                for p_id, player in players["AWAY"].items():
                    if player.jersey_no == str(away_player["jerseyNum"]):
                        # player_id = p_id
                        break

                away_player_x = away_player.get("x") if away_player else None
                away_player_y = away_player.get("y") if away_player else None

                player_data = PlayerData(
                    coordinates=Point(away_player_x, away_player_y)
                )
                players_data[player] = player_data

        
        return Frame(
            frame_id=frame_id,
            timestamp=frame_timestamp,
            ball_coordinates=ball_coordinates,
            players_data=players_data,
            period=periods[frame_period],
            ball_state=None,
            ball_owning_team=ball_owning_team,
            other_data={},
        )

    @classmethod
    def __get_periods(cls, tracking, frame_rate):
        """Gets the Periods contained in the tracking data"""        
        periods = {}
        frames_by_period = defaultdict(list)
        
        for frame in tracking:
            if frame["period"] is not None:
                frames_by_period[frame["period"]].append(frame)
        
        for period, frames in frames_by_period.items():
            periods[period] = Period(
                id=period,
                start_timestamp=timedelta(seconds=frames[0]["frameNum"] / frame_rate),
                end_timestamp=timedelta(seconds=frames[-1]["frameNum"] / frame_rate),
            )
            
        return periods

    def __load_json_raw(self, file_path):
        '''Load raw JSON file'''
        data = list()
        with bz2.open(file_path, "rt") as file:
            for i, line in enumerate(file):
                # improve reading speed by cutting of first loading step
                if self.limit and i >= (self.limit / self.sample_rate):
                    break
                data.append(json.loads(line))

        return data

    def __read_csv(self, file):
        '''Load CSV file'''
        # Read the content of the BufferedReader
        file_bytes = file.read()

        # Decode bytes to a string
        file_str = file_bytes.decode("utf-8")

        # Use StringIO to turn the string into a file-like object
        file_like = io.StringIO(file_str)

        return list(csv.DictReader(file_like))
    

    def __check_att_direction(self, et_frames, check_frames_counter):
        '''Check attacking team direction'''
        
        possible_attacking_directions = defaultdict(int)
        
        # Iterate over the required frames
        for i in range(check_frames_counter):
            attacking_direction = attacking_direction_from_frame(et_frames[i])
            possible_attacking_directions[attacking_direction] += 1
        
    
        # Return attacking_direction
        return max(possible_attacking_directions, key=possible_attacking_directions.get)
       
       
    def deserialize(self, inputs: PFF_TrackingInputs) -> TrackingDataset:
        # Load datasets
        metadata = self.__read_csv(inputs.meta_data)
        roster_meta_data = self.__read_csv(inputs.roster_meta_data)
        raw_data = self.__load_json_raw(inputs.raw_data)

        # Obtain game_id from raw data
        game_id = int(raw_data[0]["gameRefId"])

        # Filter metadata for the specific game_id
        metadata = [row for row in metadata if int(row["id"]) == game_id][0]
        
        
        if not metadata:
            raise ValueError(
                "The game_id of this game is not contained within the provided metadata.csv"
            )

        # Get metadata variables
        home_team = json.loads(metadata["homeTeam"].replace("'", '"'))
        away_team = json.loads(metadata["awayTeam"].replace("'", '"'))
        stadium = json.loads(metadata["stadium"].replace("'", '"'))
        video_data = json.loads(metadata["videos"].replace("'", '"'))
        
        # Obtain frame rate
        frame_rate = video_data["fps"]

        roster_meta_data = [
            row for row in roster_meta_data if int(row["game_id"]) == game_id
        ]

        home_team_id = home_team["id"]
        away_team_id = away_team["id"]

        with performance_logging("Loading metadata", logger=logger):
            periods = self.__get_periods(raw_data, frame_rate)

            pitch_size_width = stadium["pitchWidth"]
            pitch_size_length = stadium["pitchLength"]

            transformer = self.get_transformer(
                pitch_length=pitch_size_length, pitch_width=pitch_size_width
            )

            date = metadata.get("date")

            if date:
                date = parse(date).replace(tzinfo=timezone.utc)

            players = {"HOME": {}, "AWAY": {}}

            # Create Team objects for home and away sides
            home_team = Team(
                team_id=home_team_id,
                name=home_team["name"],
                ground=Ground.HOME,
            )
            away_team = Team(
                team_id=away_team_id,
                name=away_team["name"],
                ground=Ground.AWAY,
            )
            teams = [home_team, away_team]

            for player in roster_meta_data:
                team_id = json.loads(player["team"].replace("'", '"'))["id"]
                player_col = json.loads(player["player"].replace("'", '"'))

                player_id = player_col["id"]
                player_name = player_col["nickname"]
                shirt_number = player["shirtNumber"]
                player_position = player["positionGroupType"]

                if team_id == home_team_id:
                    team_string = "HOME"
                    team = home_team
                elif team_id == away_team_id:
                    team_string = "AWAY"
                    team = away_team
                    
                # Create Player object
                players[team_string][player_id] = Player(
                    player_id=player_id,
                    team=team,
                    jersey_no=shirt_number,
                    name=player_name,
                    starting_position=position_types_mapping.get(
                        player_position
                    ),
                )

            home_team.players = list(players["HOME"].values())
            away_team.players = list(players["AWAY"].values())

        # Check if home team plays left or right and assign orientation accordingly.
        if "homeTeamStartLeft" not in metadata:
            raise KeyError("The key 'homeTeamStartLeft' does not exist in metadata.")
            
        orientation = Orientation.HOME_AWAY if metadata.get("homeTeamStartLeft") else Orientation.AWAY_HOME
        first_period_attacking_direction = AttackingDirection.LTR if metadata.get("homeTeamStartLeft") else AttackingDirection().RTL
        
        with performance_logging("Loading data", logger=logger):

            def _iter():
                n = 0
                sample = 1.0 / self.sample_rate

                for frame in raw_data:
                    # Identify Period
                    frame_period = frame.get("period")
                    
                    # Find ball owning team
                    game_event = frame.get("game_event")
                    
                    if game_event:
                        if game_event.get("home_ball") is not None:
                            self._ball_owning_team = home_team if game_event["home_ball"] else away_team
                
                    if frame_period is not None:
                        if n % sample == 0:
                            yield frame, frame_period
                        n += 1

        frames = []
        et_frames = []

        n_frames = 0
        for _frame, _frame_period in _iter():
            # Create Frame object
            frame = self._get_frame_data(
                    teams,
                    players,
                    periods,
                    self._ball_owning_team,
                    _frame,
            )
            
            # if Regular Time  
            if _frame_period in {1, 2}:
                frames.append(frame)
                
            # else if extra time
            elif _frame_period in {3, 4}:
                et_frames.append(frame)
            
            n_frames += 1

            if self.limit and n_frames >= self.limit:
                break
                    
        if et_frames:
            et_attacking_direction = self.__check_att_direction(et_frames, check_frames_counter = 25)
           
            # If first period and third period attacking direction for home team is inconsistent, flip the direction of the extra time frames
            if first_period_attacking_direction != et_attacking_direction:
                for et_frame in et_frames:
                    # Loop through each PlayerData in the players_data dictionary
                    for player, player_data in et_frame.players_data.items():
                        if player_data.coordinates and player_data.coordinates.x is not None and player_data.coordinates.y is not None:
                            # Create a new Point with multiplied coordinates for each player
                            player_data.coordinates = Point(-player_data.coordinates.x, -player_data.coordinates.y)  
                    
                    # Multiply the x and y coordinates of the ball by -1
                    if et_frame.ball_coordinates and et_frame.ball_coordinates.x is not None and et_frame.ball_coordinates.y is not None:
                        et_frame.ball_coordinates = Point3D(-et_frame.ball_coordinates.x, -et_frame.ball_coordinates.y, et_frame.ball_coordinates.z)
                    
        frames.extend(et_frames)
            
        metadata = Metadata(
            teams=teams,
            periods=sorted(periods.values(), key=lambda p: p.id),
            pitch_dimensions=transformer.get_to_coordinate_system().pitch_dimensions,
            frame_rate=frame_rate,
            orientation=orientation,
            provider=Provider.PFF,
            flags=~(DatasetFlag.BALL_STATE | DatasetFlag.BALL_OWNING_TEAM),
            coordinate_system=transformer.get_to_coordinate_system(),
            date=date,
            game_id=game_id,
        )

        return TrackingDataset(
            records=frames,
            metadata=metadata,
        )
