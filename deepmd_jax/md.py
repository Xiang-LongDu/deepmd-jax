import jax_md
import jax
import jax.numpy as jnp
import numpy as np
from utils import load_model
import flax.linen as nn
from data import compute_lattice_candidate

mass_unit_convertion = 1.036427e2 # from AMU to eV * fs^2 / Å^2
temperature_unit_convertion = 8.617333e-5 # from Kelvin to eV
pressure_unit_convertion = 6.241509e-7 # from bar to eV / Å^3

def get_energy_fn_from_potential(model, variables):
    def energy_fn(coord, cell, nbrs_nm, static_args):
        if True: # model type is energy
            E = model.apply(variables, coord, cell, static_args, nbrs_nm)[0]
        return E
    return jax.jit(energy_fn, static_argnames=('static_args'))

def get_static_args(rcut_maybe_with_buffer, type_count, box):
    use_neighbor_list = check_if_use_neighbor_list(box, rcut_maybe_with_buffer)
    if use_neighbor_list:
        static_args = nn.FrozenDict({'type_count':type_count, 'use_neighbor_list':True})
        return static_args
    else:
        lattice_args = compute_lattice_candidate(box[None], rcut_maybe_with_buffer)
        static_args = nn.FrozenDict({'type_count':type_count, 'lattice':lattice_args, 'use_neighbor_list':False})
        return static_args

def check_if_use_neighbor_list(box, rcut):
    if box.shape == (3,3):
        return False
    else:
        return False

class Simulation:
    _step_chunk_size: int = 10
    report_interval: int = 100
    step: int = 0
    dr_buffer_neighbor: float = 0.8
    dr_buffer_lattice: float = 1.
    neighbor_buffer_size: float = 1.2
    def __init__(self,
                 model_path,
                 type_idx,
                 box,
                 position,
                 mass,
                 routine,
                 dt,
                 velocity=None,
                 init_temperature=None,
                 **routine_args):
        if velocity is None and init_temperature is None:
            raise ValueError("Please either provide velocity or init_temperature to initialize velocity")
        self.energy_fn = get_energy_fn_from_potential(model_path, type_idx)
        self.dt = dt
        self.routine = routine
        self.routine_args = routine_args
        self.mass = jnp.array(np.array(mass)[np.array(type_idx)]) # AMU
        self.model, self.variables = load_model(model_path)
        self.energy_fn = get_energy_fn_from_potential(self.model, self.variables)
        type_count = np.bincount(type_idx.astype(int))
        self.type_count = np.pad(type_count, (0, self.model.params['ntypes'] - len(type_count)))
        # If orthorhombic, keep box as (3,); else keep (3,3)
        box = jnp.array(box)
        if box.size == 1:
            self.reference_box = box.item() * jnp.ones(3)
        if box.shape == (3,3) and (box == jnp.diag(jnp.diag(box))).all():
            self.reference_box = jnp.diag(box)
        # When box is variable, use fractional coordinates for shift_fn and include extra buffer for lattice selection
        if "NPT" in self.routine:
            self.displacement_fn, self.shift_fn = jax_md.space.periodic_general(self.reference_box)
            self.static_args = get_static_args(self.model.params['rcut'] + self.dr_buffer_lattice,
                                               self.type_count,
                                               self.reference_box)
        else:
            self.displacement_fn, self.shift_fn = jax_md.space.periodic(self.reference_box)
            self.static_args = get_static_args(self.model.params['rcut'],
                                               self.type_count,
                                               self.reference_box)
        if self.static_args['use_neighbor_list']:
            self.nbrs = NeighborList(self.reference_box,
                                       self.type_count,
                                       self.model.params['rcut'] + self.dr_buffer_neighbor,
                                       self.neighbor_buffer_size)
            self.nbrs.allocate(self.position)
        # Initialize according to routine;
        if self.routine == "NVE":
            self.routine_fn = jax_md.simulate.nve
        elif self.routine == "NVT_Nose_Hoover":
            self.routine_fn = jax_md.simulate.nvt_nose_hoover
            if 'temperature' not in routine_args:
                raise ValueError("Please provide extra argument 'temperature' for routine 'NVT_Nose_Hoover' in Kelvin")
            self.temperature = routine_args.pop('temperature')
            routine_args['kT'] = self.temperature * temperature_unit_convertion
        elif self.routine == "NPT_Nose_Hoover":
            self.routine_fn = jax_md.simulate.npt_nose_hoover
            if 'temperature' not in routine_args:
                raise ValueError("Please provide extra argument 'temperature' for routine 'NPT_Nose_Hoover' in Kelvin")
            if 'pressure' not in routine_args:
                raise ValueError("Please provide extra argument 'pressure' for routine 'NPT_Nose_Hoover' in bar")
            self.temperature = routine_args.pop('temperature')
            self.pressure = routine_args.pop('pressure')
            routine_args['kT'] = self.temperature * temperature_unit_convertion
            routine_args['pressure'] = self.pressure * pressure_unit_convertion
        else:
            raise NotImplementedError("routine is currently limited to 'NVE', 'NVT_Nose_Hoover', 'NPT_Nose_Hoover'")
        self.init_fn, self.apply_fn = self.routine_fn(self.energy_fn,
                                                     self.shift_fn,
                                                     dt,
                                                     **self.routine_args)
        self.state = self.init_fn(jax.random.PRNGKey(0),
                                  position,
                                  mass=self.mass * mass_unit_convertion,
                                  kT=((init_temperature if init_temperature is not None else 0)
                                      * temperature_unit_convertion),
                                  nbrs=self.nbrs.nbrs_nm if self.static_args['use_neighbor_list'] else None,
                                  static_args=self.static_args,
                                  cell=self.reference_box,
                                  **({'box':self.reference_box} if "NPT" in self.routine else {}))
        if init_temperature is None:
            self.state.set(velocity=velocity)
    
    def check_lattice_overflow(self, position, box):
        '''Overflow that requires increasing lattice candidate/buffer, not jit-compatible'''
        pass
        return False

    def check_hard_overflow(self, box):
        '''Overflow that requires disabling neighbor list, not jit-compatible'''
        if box == None: # Not variable-box
            return False
        else:
            pass
            return False
    
    def check_soft_overflow(self, position, ref_position, box):
        '''Movement over dr_buffer_neighbor/2 that requires neighbor update, jit-compatible'''
        return False
    
    def get_inner_step(self):
        def inner_step(states):
            state, nbrs, overflow = states
            npt_box = state.box if "NPT" in self.routine else None
            current_box = state.box if "NPT" in self.routine else self.reference_box
            soft_overflow = self.check_soft_overflow(state.position, nbrs.reference_position, current_box)
            nbrs = jax.lax.cond(soft_overflow,
                                lambda nbrs: nbrs.update(state.position, box=npt_box),
                                lambda nbrs: nbrs,
                                nbrs)
            state = self.apply_fn(state,
                                  cell=current_box,
                                  nbrs=nbrs.nbrs_nm if self.static_args['use_neighbor_list'] else None,
                                  static_args=self.static_args)
            is_nbr_buffer_overflow, is_lattice_overflow, is_hard_overflow = overflow
            if self.static_args['use_neighbor_list']:
                is_nbr_buffer_overflow |= nbrs.did_buffer_overflow
                is_hard_overflow |= self.check_hard_overflow(npt_box)
            else:
                is_lattice_overflow |= self.check_lattice_overflow(state.position, current_box)
            overflow = (is_nbr_buffer_overflow, is_lattice_overflow, is_hard_overflow)
            return ((state, nbrs, overflow),
                    (state.position, state.velocity))
        return inner_step

    def run(self, steps, state):
        self.inner_step_fn = self.get_inner_step()
        while steps > 0:
            # run the simulation for a chunk of steps in a lax.scan loop
            next_chunk = min(self.report_interval - self.step % self.report_interval,
                        self._step_chunk_size - self.step % self._step_chunk_size)
            states = (self.state,
                      self.nbrs if self.static_args['use_neighbor_list'] else None,
                      (False,) * 3)
            states_new, (pos, vel) = jax.lax.scan(self.inner_step_fn, states, None, next_chunk)
            state_new, nbrs_new, overflow = states_new
            is_
            if count < self._chunk_size:
                state, report = self.inner_step(count)
                count = 0
            else:
                state, report = self.inner_step(self._chunk_size)
                count -= self._chunk_size
            self.step += self._chunk_size
            if self.step % self.report_interval == 0:
                print(report)

    


def get_type_mask_fns(type_count):
    mask_fns = []
    K = jax.device_count()
    Kmask = get_mask_by_device(type_count)
    type_count_new = -(-type_count//K)
    type_idx_filled_each = np.cumsum(np.concatenate([[0], type_count_new]))
    N_each = type_idx_filled_each[-1]
    for i in range(len(type_count)):
        def mask_fn(idx, i=i):
            idx = jax.device_put(idx, jax.sharding.PositionalSharding(jax.devices()).reshape(K,1))
            cond = Kmask[:,None] * ((idx%N_each >= type_idx_filled_each[i]) * (idx%N_each < type_idx_filled_each[i+1]))
            cond *= (idx-type_idx_filled_each[i]-(idx//N_each)*(N_each-type_count_new[i]) < type_count[i]) * (idx < N_each * K)
            return jnp.where(cond, idx, N_each * K)
        mask_fns.append(mask_fn)
    return mask_fns
def get_full_mask_fn(type_count):
    Kmask = get_mask_by_device(type_count)
    Kmask_idx = np.arange(len(Kmask))[~np.array(Kmask)]
    def mask_fn(idx):
        idx = jax.device_put(idx, jax.sharding.PositionalSharding(jax.devices()).reshape(jax.device_count(),1))
        cond = Kmask[:,None] * jnp.isin(idx, Kmask_idx, invert=True)
        return jnp.where(cond, idx, len(Kmask))
    return mask_fn
class NeighborList():
    def __init__(self, box, type_count, rcut, size):
        self.type_count, self.box = tuple(type_count), box.astype(jnp.float32)
        self.mask_fns = get_type_mask_fns(np.array(type_count))
        self.mask_fn = get_full_mask_fn(np.array(type_count))
        self.rcut, self.size = rcut, size
    def canonicalize(self, coord):
        coord = (coord.astype(jnp.float32) % self.box) * (1-2e-7) + 1e-7*self.box # avoid numerical error at box boundary
        return reorder_by_device(coord, self.type_count)
    def allocate(self, coord):
        displace = space.periodic(self.box)[0]
        coord = self.canonicalize(coord)
        test_nbr = partition.neighbor_list(displace, self.box, self.rcut, capacity_multiplier=1.,
                                           custom_mask_function=self.mask_fn).allocate(coord)
        self.knbr = np.array([int(((fn(test_nbr.idx)<len(coord)).sum(1).max())*self.size) for fn in self.mask_fns])
        self.knbr = np.where(self.knbr==0, 1, self.knbr + 1 + max(int(20*(self.size-1.2)),0))
        buffer = (sum(self.knbr)+1) / test_nbr.idx.shape[1]
        print('# Neighborlist allocated with size', np.array(self.knbr)-1)
        return partition.neighbor_list(displace, self.box, self.rcut, capacity_multiplier=buffer,
                                        custom_mask_function=self.mask_fn).allocate(coord)
    def update(self, coord, nbrs):
        return nbrs.update(self.canonicalize(coord))
    def check_dr_overflow(self, coord, ref, dr_buffer):
        return (jnp.linalg.norm((coord-ref-self.box/2)
                    %self.box - self.box/2, axis=-1) > dr_buffer/2 - 0.01).any()
    def get_nm(self, nbrs):
        K = jax.device_count()
        sharding = jax.sharding.PositionalSharding(jax.devices()).reshape(K, 1)
        nbr_idx = lax.with_sharding_constraint(nbrs.idx, sharding)
        nbrs_idx = [-lax.top_k(-fn(nbr_idx), self.knbr[i])[0] for i, fn in enumerate(self.mask_fns)]
        type_count_new = [-(-self.type_count[i]//K) for i in range(len(self.type_count))]
        type_idx_new = np.cumsum([0] + list(type_count_new))
        nbrs_nm = [mlist for mlist in zip(*[split(jnp.where(nbrs < type_idx_new[-1]*K,
            nbrs - type_idx_new[i] - (nbrs//type_idx_new[-1]) * (type_idx_new[-1]-type_count_new[i]),
            type_idx_new[-1]*K), type_count_new, K=K) for i, nbrs in enumerate(nbrs_idx)])]
        overflow = jnp.array([(idx.max(axis=1)<type_idx_new[-1]*K).any() for idx in nbrs_idx]).any() | nbrs.did_buffer_overflow
        return nbrs_nm, overflow