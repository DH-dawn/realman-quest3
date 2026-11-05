#!/usr/bin/env python3
"""Local dual-camera -> cloud inference -> checked RM65 joint following.

Preview is the default. --execute sends arm/gripper targets after preflight.
"""
from __future__ import annotations
from common.paths import config_file

import argparse
import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sys
import time

from common.paths import ROOT

import numpy as np
from scipy.spatial.transform import Rotation
from recording.dual_camera_recording import DualCameraConfig
from recording.follow_recording import BufferedRecorder
from transport.follow_transport import FollowRPC
from transport.gripper_transport import GripperTransport
from teleop.realman_follow import FollowConfig, RealManModel, JointStream, TrackingMonitor, check_gripper, gripper_call
from teleop.realman_teleop import Config, preflight, validate_profile, controlled_stop, MotionPermitExpired
from .cameras import DualCameras
from .client import PolicyClient, StaleObservationError
from .planner import PolicyJointPlanner, align_chunk, make_chunk
from .protocol import ACTION_TIMING, ACTION_SEMANTICS

VERSION = 'rm65-cloud-policy-v2'


def validate_policy_gripper(state):
    try:
        check_gripper(state)
    except ValueError as exc:
        raise ValueError('夹爪状态检查未通过；原始反馈='
                         +json.dumps(state,ensure_ascii=False,allow_nan=False)) from exc


async def read_tool_configuration(robot, command):
    """Read-only diagnostic queries; never accepts set/configuration commands."""
    fields={'get_tool_voltage':('voltage_type','tool_voltage_state'),
            'get_tool_RS485_mode':('tool_RS485_mode','get_tool_RS485_mode'),
            'get_rm_plus_mode':('mode','get_rm_plus_mode')}
    if command not in fields:
        raise ValueError('Unsupported read-only tool query')
    field,identity=fields[command]
    reader,writer=await asyncio.wait_for(asyncio.open_connection(robot.robot_ip,robot.robot_port),.2)
    try:
        writer.write(json.dumps(dict(command=command)).encode()+b'\r\n')
        await asyncio.wait_for(writer.drain(),.15)
        deadline=time.monotonic()+.4
        while time.monotonic()<deadline:
            line=await asyncio.wait_for(reader.readline(),max(.001,deadline-time.monotonic()))
            if not line:raise RuntimeError('Tool query socket disconnected')
            reply=json.loads(line)
            if not isinstance(reply,dict):continue
            if field in reply and (reply.get('command') in (command,identity)
                                  or reply.get('state') in (command,identity)):
                return reply
        raise RuntimeError('Tool query response not recognized: '+command)
    finally:
        writer.close()
        with suppress(Exception):await asyncio.wait_for(writer.wait_closed(),.1)


def inspect_sdk_state(robot):
    """Cross-check the official SDK's return codes and device state, read-only."""
    from Robotic_Arm.rm_robot_interface import RoboticArm, rm_thread_mode_e

    queries=('rm_get_current_arm_state','rm_get_arm_all_state','rm_get_arm_power_state',
             'rm_get_voltage','rm_get_tool_voltage','rm_get_gripper_state')
    report=dict(robot_ip=robot.robot_ip,robot_port=robot.robot_port,
        transport='official_RM_API2_read_only',cloud_opened=False,cameras_opened=False,
        motion_commands_sent=0,queries={},query_errors={})
    with (ROOT/f'.realman-{robot.robot_ip}-{robot.robot_port}.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        arm=RoboticArm(rm_thread_mode_e.RM_DUAL_MODE_E)
        connected=False
        try:
            handle=arm.rm_create_robot_arm(robot.robot_ip,robot.robot_port)
            if handle.id<0:raise RuntimeError('官方SDK连接机械臂失败')
            connected=True
            report['handle_id']=handle.id
            for name in queries:
                try:
                    code,state=getattr(arm,name)()
                    report['queries'][name]=dict(return_code=code,state=state)
                except Exception as exc:
                    report['query_errors'][name]=f'{type(exc).__name__}: {exc}'
        finally:
            if connected:
                report['disconnect_return_code']=arm.rm_delete_robot_arm()
            arm.rm_destroy()
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),flush=True)
    if report['query_errors'] or any(value['return_code']!=0 for value in report['queries'].values()):
        raise RuntimeError('官方SDK只读查询未全部成功；非零返回码对应的状态不可当作有效反馈')
    validate_policy_gripper(report['queries']['rm_get_gripper_state']['state'])
    print('官方SDK查询成功，夹爪状态检查通过；未发送运动、供电设置或使能命令',flush=True)


def load_settings(path):
    path = path.expanduser().resolve()
    settings = json.loads(path.read_text())
    expected = {'uri','robot_config','follow_config','camera_config','prompt','execute_steps','request_timeout_s',
        'max_prediction_age_s','max_observation_age_s','camera_timeout_s','max_target_delta_deg',
        'workspace_radius_mm','max_duration_s','gripper_enabled','gripper_close_threshold','gripper_open_threshold'}
    if set(settings) != expected:
        raise ValueError('Unexpected inference configuration fields')
    for name in ('robot_config','follow_config','camera_config'):
        value = Path(settings[name]).expanduser()
        settings[name] = str((path.parent / value).resolve() if not value.is_absolute() else value)
    if not isinstance(settings['uri'],str) or not settings['uri'].startswith(('ws://','wss://')):
        raise ValueError('uri requires ws:// or wss://')
    if not isinstance(settings['prompt'],str) or not settings['prompt'].strip():
        raise ValueError('Task prompt must be nonempty')
    if type(settings['execute_steps']) is not int or not 1 <= settings['execute_steps'] <= 10:
        raise ValueError('execute_steps must be 1..10')
    if type(settings['gripper_enabled']) is not bool:
        raise ValueError('gripper_enabled must be boolean')
    limits = {'request_timeout_s':(.1,10.), 'max_prediction_age_s':(.1,1.),
        'max_observation_age_s':(.03,.1), 'camera_timeout_s':(.05,.2),
        'max_target_delta_deg':(.1,10.), 'workspace_radius_mm':(10.,400.), 'max_duration_s':(1.,7200.)}
    for key,(low,high) in limits.items():
        value=settings[key]
        if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be finite within [{low},{high}]')
    close,opening=settings['gripper_close_threshold'],settings['gripper_open_threshold']
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (close,opening)) or not 0 <= close < opening <= 1:
        raise ValueError('Gripper hysteresis requires 0 <= close < open <= 1')
    return settings


class PolicyRunner:
    def __init__(self, settings, robot, follow, camera_config, *, execute, output):
        self.s=settings;self.robot=robot;self.follow=follow;self.camera_config=camera_config
        self.execute=execute;self.output=output
        self.stop=asyncio.Event();self.authorized=False;self.chunk=None
        self.latest=None;self.profile_stamp=-math.inf;self.gripper=None;self.gripper_stamp=-math.inf
        self.feedback_history=deque(maxlen=100);self.gripper_history=deque(maxlen=100)
        self.model_lock=asyncio.Lock();self.tasks=[]
        self.rpc=self.stream=self.client=self.cameras=self.gripper_lane=self.recorder=None
        self.planner=self.tracking=None
        self.last_gripper_action=None;self.gripper_task=None;self.accepted=0
        self.authorized_at=None;self.stall_reference=None;self.stall_since=None
        self.last_rejection=None;self.last_rejection_print=0.
        self.last_alignment=None
        self.last_permit_reason=None

    def permit_failure(self):
        now=time.monotonic()
        if not self.authorized or self.stop.is_set() or self.latest is None:
            return 'Control authorization withdrawn or no robot feedback'
        if now-self.latest['received_at'] > self.robot.feedback_timeout_s:
            return f"Robot feedback age {(now-self.latest['received_at'])*1000:.1f}ms exceeds {self.robot.feedback_timeout_s*1000:g}ms"
        if now-self.profile_stamp > .5:
            return f'Hardware profile age {(now-self.profile_stamp)*1000:.1f}ms exceeds 500ms'
        if now-self.gripper_stamp > .5:
            return f'Gripper feedback age {(now-self.gripper_stamp)*1000:.1f}ms exceeds 500ms'
        try:
            self.cameras.check(stale_after=self.s['camera_timeout_s'])
            self.rpc.check()
        except Exception as exc:
            return f'Camera/robot transport: {exc}'
        if self.client.socket is None or self.client.socket.state.name != 'OPEN':
            state=self.client.socket.state.name if self.client.socket else 'NONE'
            return f'Cloud WebSocket is {state}'
        return None

    def permit(self):
        self.last_permit_reason=self.permit_failure()
        return self.last_permit_reason is None

    def task_finished(self, task):
        if not task.cancelled():
            error=task.exception()
            if error is not None:
                self.recorder.event('policy_task_failed',task=task.get_name(),
                                    error=f'{type(error).__name__}: {error}')

    async def feedback_loop(self):
        while not self.stop.is_set():
            snap=await self.rpc.snapshot();finished=time.monotonic()
            q=np.asarray(snap['state']['joint_deg'])
            self.planner.check_pose(np.asarray(snap['state']['tcp_pose_m_rad']))
            if np.any(q < self.planner.low+self.follow.joint_margin_deg) or np.any(q > self.planner.high-self.follow.joint_margin_deg):
                raise ValueError('Measured joints reached limit margin')
            if self.execute:
                self.tracking.observe(snap['received_at'],q)
                if np.max(np.abs(q-self.planner.q)) > .3:
                    if self.stall_reference is None or np.max(np.abs(q-self.stall_reference)) > .05:
                        self.stall_reference=q.copy();self.stall_since=finished
                    elif self.stall_since is not None and finished-self.stall_since > 1.:
                        raise RuntimeError('No measured joint response to policy commands')
                else:
                    self.stall_reference=q.copy();self.stall_since=None
            self.latest=snap
            self.feedback_history.append((finished,snap))
            self.recorder.event('policy_feedback',query_started=snap['received_at'],query_finished=finished,
                                state=snap['state'],trajectory=snap['trajectory'])
            await asyncio.sleep(.02)

    async def profile_loop(self):
        while not self.stop.is_set():
            profile=await self.rpc.profile();validate_profile(self.robot,profile)
            self.profile_stamp=profile['_started_at']
            await asyncio.sleep(.2)

    async def read_gripper(self, phase):
        started=time.monotonic()
        state=await gripper_call(self.robot,'get_gripper_state',self.follow)
        finished=time.monotonic()
        # Preserve the failing packet too, including during startup.
        self.recorder.event('policy_gripper_feedback',phase=phase,
            query_started=started,query_finished=finished,state=state)
        validate_policy_gripper(state)
        return state,started,finished

    async def gripper_feedback_loop(self):
        while not self.stop.is_set():
            state,started,finished=await self.read_gripper('running')
            self.gripper,self.gripper_stamp=state,started
            self.gripper_history.append((finished,started,state))
            await asyncio.sleep(.05)

    def observation(self):
        pair=self.cameras.pair(max_age=self.s['max_observation_age_s'])
        if pair is None:
            return None
        images,oldest,reference=pair
        robot=next((snap for finished,snap in reversed(self.feedback_history) if finished <= reference),None)
        grip=next(((started,state) for finished,started,state in reversed(self.gripper_history) if finished <= reference),None)
        if robot is None or grip is None:
            return None
        captured_at=min(oldest,robot['received_at'],grip[0])
        if not 0 <= time.monotonic()-captured_at <= self.s['max_observation_age_s']:
            return None
        state=np.r_[robot['state']['joint_rad'],np.clip(grip[1]['actpos']/1000.,0.,1.)].astype(np.float32)
        # Training host_reference is the later camera, not the oldest input.
        return {**images,'observation/state':state,'prompt':self.s['prompt']},captured_at,reference

    async def policy_loop(self):
        while not self.stop.is_set():
            observation=self.observation()
            if observation is None:
                await asyncio.sleep(.005)
                continue
            obs,captured_at,reference_at=observation
            try:
                result=await self.client.infer(obs,captured_at=captured_at,
                                               max_observation_age_s=self.s['max_observation_age_s'])
            except StaleObservationError as exc:
                self.recorder.event('policy_observation_dropped',captured_at=captured_at,
                                    reference_at=reference_at,reason=str(exc))
                await asyncio.sleep(.005)
                continue
            received_at=time.monotonic()
            self.recorder.event('policy_prediction',captured_at=captured_at,reference_at=reference_at,received_at=received_at,
                request_id=result['request_id'],session_id=result['session_id'],
                actions=result['actions'].tolist(),timing=result['client_timing'],
                sample_rate_hz=result['sample_rate_hz'],action_timing=result['action_timing'])
            try:
                async with self.model_lock:
                    selected_at=time.monotonic()
                    operation=asyncio.create_task(asyncio.to_thread(make_chunk,result,captured_at,received_at,reference_at,selected_at,
                        np.asarray(self.latest['state']['joint_deg']),self.planner,self.s))
                    try:chunk=await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        with suppress(Exception):await operation
                        raise
                adopted_at=time.monotonic()
                if chunk.target(adopted_at,self.s['max_prediction_age_s']) is None:
                    raise ValueError('Aligned action window expired during validation')
            except ValueError as exc:
                self.recorder.event('policy_prediction_rejected',request_id=result['request_id'],reason=str(exc))
                if str(exc)!=self.last_rejection or time.monotonic()-self.last_rejection_print>=1.:
                    print(f'预测未采用：{exc}；继续等待新预测',flush=True)
                    self.last_rejection_print=time.monotonic()
                self.last_rejection=str(exc)
            else:
                self.chunk=chunk;self.accepted+=1;self.last_rejection=None
                first_index=chunk.index_at(adopted_at,self.s['max_prediction_age_s'])
                self.last_alignment=(adopted_at-reference_at)*1000,first_index
                self.recorder.event('policy_chunk_accepted',request_id=chunk.request_id,steps=len(chunk.actions),
                    captured_at=captured_at,reference_at=reference_at,received_at=received_at,adopted_at=adopted_at,
                    age_ms=(adopted_at-captured_at)*1000,alignment_delay_ms=(adopted_at-reference_at)*1000,
                    skipped_steps=first_index,validation_start_index=chunk.start_index,first_action_index=first_index,
                    expires_at=chunk.expires_at(self.s['max_prediction_age_s']))
            # Use fresh sensor data for the next request; no queued observation replay.
            await asyncio.sleep(.01)

    async def control_loop(self):
        period=1/self.follow.control_hz;deadline=time.monotonic();last_print=0.;last_control=None
        while not self.stop.is_set():
            now=time.monotonic()
            if last_control is not None and now-last_control > .08:
                raise RuntimeError('Policy control scheduler exceeded 80ms')
            last_control=now
            self.recorder.check()
            if not self.permit():
                raise RuntimeError('Policy permit expired: '+self.last_permit_reason)
            if self.gripper_task is not None and self.gripper_task.done():
                try:self.gripper_task.result()
                except MotionPermitExpired:
                    self.last_gripper_action=None
                    self.recorder.event('policy_gripper_intent_expired')
                self.gripper_task=None
            if self.chunk is None and now-self.authorized_at > self.s['request_timeout_s']+1.:
                raise RuntimeError('No initial usable cloud prediction within startup deadline')
            if self.chunk and now-self.chunk.captured_at > self.s['request_timeout_s']+1.:
                raise RuntimeError('No accepted fresh prediction; policy stream stopped')
            async with self.model_lock:
                # Waiting for model validation also consumes prediction lifetime.
                sampled_at=time.monotonic();packet=self.chunk
                row_index=packet.index_at(sampled_at,self.s['max_prediction_age_s']) if packet else None
                row=packet.actions[row_index-packet.start_index] if row_index is not None else None
                q_goal=np.rad2deg(row[:6]) if row is not None else None
                previous_q=self.planner.q.copy();previous_velocity=self.planner.velocity.copy()
                previous_reason=self.planner.limiting_reason
                operation=asyncio.create_task(asyncio.to_thread(self.planner.step_joint,q_goal,period,
                    self.latest['state']['joint_deg'],self.latest['received_at']))
                try:q,pose=await asyncio.shield(operation)
                except asyncio.CancelledError:
                    with suppress(Exception):await operation
                    raise
            def goal_permit():
                return self.permit() and (row_index is None or (packet is self.chunk
                    and packet.index_at(time.monotonic(),self.s['max_prediction_age_s'])==row_index))
            submitted=False
            try:
                # Rechecked by transport after its rate-limit/write-lock waits too.
                if not goal_permit():
                    raise MotionPermitExpired('Aligned action changed/expired before write')
                if self.execute:
                    await self.stream.send(q,goal_permit)
                    self.tracking.command(time.monotonic(),q)
                else:
                    self.recorder.event('policy_preview_joint',joint_deg=q.tolist(),tcp_model_m_rad=pose.tolist())
                submitted=True
                self.recorder.event('policy_control_target',request_id=packet.request_id if packet else None,
                    action_index=row_index,sampled_at=sampled_at,reference_at=packet.reference_at if packet else None,
                    target_joint_deg=q_goal.tolist() if q_goal is not None else None,joint_deg=q.tolist(),
                    mode='follow' if row_index is not None else 'brake_hold',execute=self.execute)
            except MotionPermitExpired:
                # Nothing was written. Keep the trajectory at its last committed state.
                self.planner.q=previous_q;self.planner.velocity=previous_velocity
                self.planner.limiting_reason=previous_reason
                self.recorder.event('policy_action_expired_before_write',
                    request_id=packet.request_id if packet else None,action_index=row_index)
                if not self.permit():
                    raise RuntimeError('Permit expired before policy write: '+self.last_permit_reason)
            if submitted and row is not None and self.s['gripper_enabled'] and self.execute and self.gripper_task is None:
                action=('close' if row[6] <= self.s['gripper_close_threshold'] else
                        'open' if row[6] >= self.s['gripper_open_threshold'] else None)
                if action and action != self.last_gripper_action:
                    def gripper_permit(packet=packet,row_index=row_index):
                        return self.permit() and packet is self.chunk and packet.index_at(time.monotonic(),self.s['max_prediction_age_s'])==row_index
                    self.last_gripper_action=action
                    command='set_gripper_pick' if action=='close' else 'set_gripper_release'
                    self.gripper_task=asyncio.create_task(self.gripper_lane.send(command,gripper_permit))
                    self.recorder.event('policy_gripper_request',action=action,request_id=packet.request_id,action_index=row_index)
            if now-last_print >= .5:
                alignment=(f'align={self.last_alignment[0]:.0f}ms/skip={self.last_alignment[1]}'
                           if self.last_alignment else '等待首个对齐预测')
                print(f"{'EXECUTE' if self.execute else 'PREVIEW'} {'FOLLOW' if row is not None else 'BRAKE/HOLD'} "
                    f"predictions={self.accepted} sent={self.stream.sent} lead={self.tracking.latest_error:.2f}deg "
                    f"action={row_index if row_index is not None else '-'} {alignment} "
                    f"limit={self.planner.limiting_reason or 'none'}",flush=True)
                last_print=now
            deadline+=period
            if deadline < time.monotonic():deadline=time.monotonic()+period
            await asyncio.sleep(max(0.,deadline-time.monotonic()))

    async def run(self):
        self.recorder=BufferedRecorder(self.output)
        lock=None;report={'version':VERSION,'execute':self.execute,'actual_hardware_acceptance':False}
        try:
            lock=(ROOT/f'.realman-{self.robot.robot_ip}-{self.robot.robot_port}.lock').open('a+')
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.rpc=FollowRPC(self.robot,self.execute,self.recorder)
            self.stream=JointStream(self.rpc,self.recorder,1/self.follow.control_hz)
            prepared=await preflight(self.rpc)
            model=RealManModel(*prepared['bounds']);initial=prepared['anchor'];fk=model.fk(initial['joint_deg'])
            error=np.linalg.norm(fk[:3]-initial['tcp_pose_m_rad'][:3])
            angle=(Rotation.from_euler('xyz',fk[3:])*Rotation.from_euler('xyz',initial['tcp_pose_m_rad'][3:]).inv()).magnitude()
            if error > .005 or angle > math.radians(2):
                raise ValueError('RM65 FK/tool frame does not match actual feedback')
            model.check(initial['joint_deg'])
            self.planner=PolicyJointPlanner(self.follow,model,initial['joint_deg'],prepared['bounds'])
            self.tracking=TrackingMonitor(self.follow)
            self.latest=await self.rpc.snapshot()
            self.profile_stamp=(await self.rpc.profile())['_started_at']
            self.gripper,self.gripper_stamp,_=await self.read_gripper('startup')
            self.gripper_lane=GripperTransport(self.robot,self.follow,execute=self.execute,recorder=self.recorder)
            self.client=PolicyClient(self.s['uri'],timeout_s=self.s['request_timeout_s'],token=os.environ.get('RM65_INFER_TOKEN'))
            hello=await self.client.connect()
            if (hello.get('sample_rate_hz') != 10 or hello.get('action_semantics') != ACTION_SEMANTICS
                    or hello.get('action_timing') != ACTION_TIMING):
                raise ValueError('Cloud model action semantics do not match this executor')
            self.cameras=DualCameras(self.camera_config,ROOT);await self.cameras.start(self.stop)
            # Camera startup takes time; refresh hardware before authorizing any sends.
            self.latest=await self.rpc.snapshot();profile=await self.rpc.profile();validate_profile(self.robot,profile)
            self.profile_stamp=profile['_started_at']
            self.gripper,self.gripper_stamp,_=await self.read_gripper('before_authorization')
            current_q=np.asarray(self.latest['state']['joint_deg'])
            if (self.latest['trajectory']['type']!='none'
                    or np.max(np.abs(current_q-np.asarray(initial['joint_deg'])))>.05):
                raise RuntimeError('Robot moved during policy startup; restart before executing')
            model.check(current_q)
            self.planner.reset(current_q)
            self.planner.observe(current_q,self.latest['received_at'])
            self.recorder.event('policy_started',settings=self.s,robot=vars(self.robot),
                follow=vars(self.follow),checkpoint=hello['checkpoint'],execute=self.execute)
            print(f"{VERSION} | {'真机执行' if self.execute else '只读预览'} | {self.s['uri']} | "
                  f"movej_follow {self.follow.control_hz:g}Hz | 采集时间对齐、跳过过时动作后最多{self.s['execute_steps']}步 | "
                  f"半径{self.follow.workspace_radius_mm:g}mm；Ctrl+C停止",flush=True)
            if self.stop.is_set():
                raise InterruptedError("Stopped during policy startup")
            self.authorized=True;self.authorized_at=time.monotonic()
            self.tasks=[asyncio.create_task(fn(),name=fn.__name__) for fn in
                (self.feedback_loop,self.profile_loop,self.gripper_feedback_loop,self.policy_loop,self.control_loop)]
            for task in self.tasks:task.add_done_callback(self.task_finished)
            waiter=asyncio.create_task(self.stop.wait())
            timer=asyncio.create_task(asyncio.sleep(self.s['max_duration_s']))
            try:
                done,_=await asyncio.wait([*self.tasks,waiter,timer],return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task in self.tasks:task.result()
                report['status']='operator_or_duration'
            finally:
                waiter.cancel();timer.cancel()
                await asyncio.gather(waiter,timer,return_exceptions=True)
        except BaseException as exc:
            report.update(status='failed',error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            self.authorized=False;self.stop.set()
            for task in self.tasks:task.cancel()
            if self.gripper_task is not None:self.gripper_task.cancel()
            if self.stream is not None:await self.stream.close()
            if self.execute and self.rpc is not None and self.rpc.motion_attempts:
                try:
                    await controlled_stop(self.rpc,self.recorder)
                    report['stop_verified']=True
                except Exception as exc:
                    report.update(stop_verified=False,stop_error=str(exc))
            await asyncio.gather(*self.tasks,return_exceptions=True)
            if self.gripper_task is not None:await asyncio.gather(self.gripper_task,return_exceptions=True)
            for resource in (self.client,self.cameras,self.gripper_lane,self.rpc):
                if resource is not None:
                    try:await resource.close()
                    except Exception as exc:report.setdefault('cleanup_errors',[]).append(str(exc))
            report['motion_commands_sent']=self.stream.sent if self.stream else 0
            self.recorder.event('policy_finished',**report)
            report['recording']=await asyncio.to_thread(self.recorder.close)
            (self.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            if lock:lock.close()
            print(f"本地推理结束：{report.get('status')}；记录：{self.output}",flush=True)
            if report.get('stop_verified') is False:
                raise RuntimeError('STOP未确认，请使用实体急停并检查示教器：'+report.get('stop_error',''))


def replay_events(path, settings, follow, output, delay_ms=None):
    """Inspect recorded predictions on the production time selector; no device I/O.

    This is a timing/units diagnostic, not a kinematic or model acceptance check.
    A delay override shifts reception only, never the observation's reference.
    """
    path=path.expanduser().resolve()
    output.mkdir(parents=True,exist_ok=False)
    report=dict(version=VERSION,source=str(path),delay_override_ms=delay_ms,
        devices_opened=False,motion_commands_sent=0,kinematics_checked=False,
        predictions=0,accepted=0,rejected=0,legacy_reference_assumed=0,
        skipped_steps=0,active_samples=0,brake_samples=0)
    with path.open(encoding='utf-8') as source, (output/'alignment_replay.jsonl').open('x',encoding='utf-8') as stream:
        for line in source:
            event=json.loads(line)
            if event.get('event')=='policy_started':
                report['source_checkpoint']=event.get('checkpoint')
            if event.get('event')!='policy_prediction':continue
            report['predictions']+=1
            captured=event['captured_at'];reference=event.get('reference_at',captured)
            legacy='reference_at' not in event
            report['legacy_reference_assumed']+=int(legacy)
            received=event['received_at'] if delay_ms is None else reference+delay_ms/1000
            # Original event emission includes response decoding/log overhead.
            selected=max(received,event.get('monotonic',received)) if delay_ms is None else received
            result=dict(event,sample_rate_hz=event.get('sample_rate_hz',10),
                        action_timing=event.get('action_timing',ACTION_TIMING))
            row=dict(request_id=event['request_id'],reference_at=reference,captured_at=captured,
                received_at=received,selected_at=selected,legacy_reference_assumed=legacy)
            try:
                chunk=align_chunk(result,captured,received,reference,selected,settings)
                report['accepted']+=1;report['skipped_steps']+=chunk.start_index
                samples=[]
                for tick in range(math.ceil((chunk.expires_at(settings['max_prediction_age_s'])-selected)*follow.control_hz)+3):
                    now=selected+tick/follow.control_hz
                    index=chunk.index_at(now,settings['max_prediction_age_s'])
                    action=chunk.target(now,settings['max_prediction_age_s'])
                    samples.append(dict(at=now,action_index=index,
                        joint_deg=np.rad2deg(action[:6]).tolist() if action is not None else None,
                        gripper_opening=float(action[6]) if action is not None else None))
                    report['active_samples' if action is not None else 'brake_samples']+=1
                row.update(status='aligned',skipped_steps=chunk.start_index,
                    retained_steps=len(chunk.actions),expires_at=chunk.expires_at(settings['max_prediction_age_s']),samples=samples)
            except ValueError as exc:
                report['rejected']+=1;row.update(status='rejected',reason=str(exc))
            stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
    if not report['predictions']:raise ValueError('日志中没有policy_prediction，不能回放动作时间轴')
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    print(f'时间对齐回放已保存：{output}；未连接云端、相机或机械臂',flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=config_file('infer.json'))
    parser.add_argument('--uri',help='例如ws://127.0.0.1:8000或ws://云端IP:8000')
    parser.add_argument('--execute',action='store_true',help='预检通过后实际驱动机械臂；默认只预览')
    parser.add_argument('--seconds',type=float)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--check-only',action='store_true',help='仅检查本地配置，不打开相机/机器人/网络')
    parser.add_argument('--check-gripper',action='store_true',help='只读查询真实夹爪状态，不连接云端、不打开相机、不发送运动命令')
    parser.add_argument('--check-sdk',action='store_true',help='用官方RM_API2只读对照查询机械臂、控制器供电、工具供电及夹爪状态')
    parser.add_argument('--replay-events',type=Path,help='离线回放events.jsonl中预测的时间对齐，不打开任何设备')
    parser.add_argument('--replay-delay-ms',type=float,help='回放时覆盖采集参考时刻到接收的延迟，单位ms')
    args=parser.parse_args(argv)
    if args.check_sdk and (args.execute or args.check_only or args.check_gripper or args.replay_events):
        parser.error('--check-sdk不能与其他执行/检查/回放模式组合')
    if args.check_gripper and (args.execute or args.check_only or args.replay_events):
        parser.error('--check-gripper不能与--execute、--check-only或--replay-events组合')
    if args.replay_events and (args.execute or args.check_only):
        parser.error('--replay-events不能与--execute或--check-only组合')
    if args.replay_delay_ms is not None and (not args.replay_events or not math.isfinite(args.replay_delay_ms) or args.replay_delay_ms<0):
        parser.error('--replay-delay-ms需要--replay-events，且必须为有限非负数')
    settings=load_settings(args.config)
    if args.uri:
        if not args.uri.startswith(('ws://','wss://')):parser.error('uri requires ws:// or wss://')
        settings['uri']=args.uri
    if args.seconds is not None:
        if not math.isfinite(args.seconds) or not 1 <= args.seconds <= settings['max_duration_s']:
            parser.error('seconds must be within [1, configured max_duration_s]')
        settings['max_duration_s']=args.seconds
    robot=Config.load(Path(settings['robot_config']))
    follow=replace(FollowConfig.load(Path(settings['follow_config'])),workspace_radius_mm=settings['workspace_radius_mm']).validate()
    cameras=DualCameraConfig.load(Path(settings['camera_config']))
    if args.check_sdk:
        inspect_sdk_state(robot)
        return
    if args.check_gripper:
        async def inspect_gripper():
            state=await gripper_call(robot,'get_gripper_state',follow)
            diagnostics={}
            for command in ('get_tool_voltage','get_tool_RS485_mode','get_rm_plus_mode'):
                try:diagnostics[command]=await read_tool_configuration(robot,command)
                except Exception as exc:
                    diagnostics[command]=dict(query_failed=True,error=f'{type(exc).__name__}: {exc}')
            print(json.dumps(dict(robot_ip=robot.robot_ip,robot_port=robot.robot_port,
                gripper=state,tool_diagnostics=diagnostics,
                cloud_opened=False,cameras_opened=False,motion_commands_sent=0),
                ensure_ascii=False,indent=2),flush=True)
            voltage_type=diagnostics['get_tool_voltage'].get('voltage_type')
            if voltage_type in (0,2,3):
                volts={0:0,2:12,3:24}[voltage_type]
                print(f'工具端供电设置：{volts}V'
                      '（控制器配置值，不代表实测电压）',flush=True)
            validate_policy_gripper(state)
            print('夹爪状态检查通过；未发送运动或夹爪开合命令',flush=True)
        asyncio.run(inspect_gripper())
        return
    if args.check_only:
        print(json.dumps(dict(version=VERSION,settings=settings,robot_ip=robot.robot_ip,
            control_hz=follow.control_hz,cameras=cameras.cameras,devices_opened=False),ensure_ascii=False,indent=2))
        return
    output=args.output.expanduser().resolve() if args.output else ROOT/'run'/datetime.now().strftime('infer_%Y%m%dT%H%M%S_%f')
    if args.replay_events:
        replay_events(args.replay_events,settings,follow,output,args.replay_delay_ms)
        return
    runner=PolicyRunner(settings,robot,follow,cameras,execute=args.execute,output=output)
    async def run():
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):loop.add_signal_handler(sig,runner.stop.set)
        await runner.run()
    asyncio.run(run())


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as exc:raise SystemExit(str(exc))
