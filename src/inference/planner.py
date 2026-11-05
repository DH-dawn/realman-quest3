"""Direct joint-action planning, reusing the checked follow planner's braking rollout."""
from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
from teleop.realman_follow import JointPlanner, PlanningConstraint
from .protocol import ACTION_TIMING


@dataclass(frozen=True)
class ActionChunk:
    actions: np.ndarray
    captured_at: float
    received_at: float
    reference_at: float
    start_index: int
    request_id: int
    session_id: str
    rate_hz: float = 10.

    def index_at(self, now, max_age):
        if not math.isfinite(now) or not 0 <= now - self.captured_at < max_age:
            return None
        index = math.floor((now - self.reference_at) * self.rate_hz)
        return index if self.start_index <= index < self.start_index + len(self.actions) else None

    def target(self, now, max_age):
        index = self.index_at(now, max_age)
        return self.actions[index - self.start_index] if index is not None else None

    def expires_at(self, max_age):
        return min(self.captured_at + max_age,
                   self.reference_at + (self.start_index + len(self.actions)) / self.rate_hz)


class PolicyJointPlanner(JointPlanner):
    def validate_goal(self, joint_deg, reference, max_delta):
        q = np.asarray(joint_deg, dtype=float)
        if q.shape != (6,) or not np.isfinite(q).all():
            raise ValueError('Policy joint target must contain six finite values')
        self._check_joint_margin(q, planning=True)
        delta=np.abs(q-reference)
        if np.max(delta) > max_delta:
            joint=int(np.argmax(delta))
            raise ValueError(f'Prediction J{joint+1} differs {delta[joint]:.2f}deg from measured joints '
                             f'(target={q[joint]:.2f}, measured={reference[joint]:.2f}, limit={max_delta:g}deg)')
        self.check_pose(self.model.fk(q))
        self.model.check(q)

    def step_joint(self, target_deg, dt, measured_q, measured_at):
        """None means brake on the previously validated stop path; never reset velocity."""
        if not np.isfinite(dt) or not 0 < dt <= .05:
            raise ValueError('Invalid control period')
        dt = min(dt, 1 / self.c.control_hz)
        self.observe(measured_q, measured_at)
        pose = self.model.fk(self.q)
        if not self.state_checked:
            self.model.check(self.q)
            self._check_joint_margin(self.q)
            self.state_checked = True
        if target_deg is None:
            requested = np.zeros(6)
        else:
            target_deg = np.asarray(target_deg, dtype=float)
            self._check_joint_margin(target_deg, planning=True)
            requested = (target_deg - self.q) / dt
            requested *= min(1., self.c.joint_speed_deg_s / max(np.max(np.abs(requested)), 1e-12))
        # Same feedback lead governor as JointPlanner._proposal, without solving IK.
        if self.measured_at is not None:
            direction = np.sign(requested)
            lead = (self.q - self.measured) * direction
            relative_speed = np.maximum(0., (self.velocity - self.measured_velocity) * direction)
            maximum_lead = min(3., self.c.joint_tracking_error_deg * .45)
            headroom = np.maximum(0., maximum_lead - lead - relative_speed * dt
                                  - .5 * self.c.joint_accel_deg_s2 * dt * dt)
            cap = np.maximum(0., self.measured_velocity * direction) + np.sqrt(2 * self.c.joint_accel_deg_s2 * headroom)
            moving = np.abs(requested) > 1e-6
            scale = float(np.min(np.minimum(np.abs(requested), cap)[moving] / np.abs(requested[moving]))) if np.any(moving) else 1.
            requested *= scale
        fraction = min(1., self.c.joint_accel_deg_s2 * dt / max(np.max(np.abs(requested - self.velocity)), 1e-12))
        velocity = self.velocity + fraction * (requested - self.velocity)
        braking = self._brake(self.velocity, dt)
        reason = ''
        for fraction in (1., .5, .25, .125, .0625, 0.):
            candidate = braking + fraction * (velocity - braking)
            try:
                q, actual_pose = self._check_braking_path(pose, candidate, dt)
            except PlanningConstraint as exc:
                reason = exc.kind
                continue
            self.q, self.velocity = q, candidate
            self.limiting_reason = reason if fraction < 1. else ''
            return q.copy(), actual_pose
        raise ValueError('No validated policy braking path: ' + reason)


def align_chunk(result, captured_at, received_at, reference_at, selected_at, settings):
    """Discard elapsed intervals, then select a future prefix on the observation clock.

    captured_at is the oldest participating input (freshness bound). reference_at
    is the later camera reception, matching the training converter host_reference.
    Row i applies on [reference+i/10, reference+(i+1)/10), even after reception.
    """
    actions = np.asarray(result['actions'], dtype=float)
    if actions.ndim != 2 or actions.shape[1] != 7 or not np.isfinite(actions).all():
        raise ValueError('Invalid cloud action array')
    if (not all(math.isfinite(t) for t in (captured_at, reference_at, received_at, selected_at))
            or not captured_at <= reference_at <= received_at <= selected_at):
        raise ValueError('Invalid local observation/prediction timeline')
    if selected_at - captured_at >= settings['max_prediction_age_s']:
        raise ValueError('Cloud prediction expired before adoption')
    if result.get('sample_rate_hz') != 10 or result.get('action_timing') != ACTION_TIMING:
        raise ValueError('Policy action timing does not match training intervals at 10Hz')
    start = math.floor((selected_at - reference_at) * 10)
    actions = actions[start:start + settings['execute_steps']].copy()
    if not len(actions):
        raise ValueError('All predicted action intervals have elapsed')
    # Small regression overshoot is mapped to the known binary gripper endpoints;
    # large out-of-domain values are rejected rather than treated as raw 0..1000.
    if np.any(actions[:, 6] < -.05) or np.any(actions[:, 6] > 1.05):
        raise ValueError('Policy gripper opening is outside the normalized range')
    actions[:, 6] = np.clip(actions[:, 6], 0., 1.)
    actions.setflags(write=False)
    return ActionChunk(actions, captured_at, received_at, reference_at, start,
                       result['request_id'], result['session_id'])


def make_chunk(result, captured_at, received_at, reference_at, selected_at, measured_q, planner, settings):
    """Apply the same physical checks to every retained row, never to elapsed rows."""
    chunk = align_chunk(result, captured_at, received_at, reference_at, selected_at, settings)
    for row in chunk.actions:
        planner.validate_goal(np.rad2deg(row[:6]), measured_q, settings['max_target_delta_deg'])
    return chunk
