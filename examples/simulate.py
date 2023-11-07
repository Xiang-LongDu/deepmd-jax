import os
# os.environ['XLA_PYTHON_CLIENT_MEM_FRACTION']='.90' 
import jax.numpy as jnp
import numpy as np
from jax import jit, random, grad
import flax.linen as nn
import jax, sys, pickle, warnings
from time import time
from jax_md import space, quantity, simulate
# Path to deepmd_jax; change it if you're running this script from a different directory
sys.path.append(os.path.abspath('../'))
from deepmd_jax.data import compute_lattice_candidate
from deepmd_jax.dpmodel import DPModel
from deepmd_jax.utils import reorder_by_device
from deepmd_jax.simulation_utils import NeighborListLoader
shard_all = jax.sharding.PositionalSharding(jax.devices()).replicate()
print('# Starting program on %d device(s):' % jax.device_count(), jax.devices())
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=UserWarning)
np.set_printoptions(precision=4, suppress=True)
precision        = '64' # '16' '32' '64', set precision here
if precision == '32':
    jax.config.update('jax_default_matmul_precision', 'float32')
if precision == '64':
    jax.config.update('jax_enable_x64', True)

# units in Angstrom, eV, fs
model_path       = 'trained_models/dp_water_1.pkl'  # path to the trained model for simulation
save_path        = './'                # make sure the path exists!
save_prefix      = 'water'             # prefix for saved files
use_model_devi   = False               # compute model deviation of different models
model_devi_paths = ['trained_models/dp_water_2.pkl', 'trained_models/dp_water_3.pkl']
dt               = 0.48                # time step (fs)
temp             = 350 * 8.61733e-5    # temperature (Kelvin * unit_convert)
mass             = np.array([15.9994, 1.00784]) * 1.036427e2 # mass by type (AMU * unit_convert)
total_steps      = 10000               # Total number of simulation steps
print_every      = 200                 # Frequency of printing and calculating model deviation
save_every       = 1                   # Frequency of recording trajectory
use_neighborlist = True                # Use only when (1) box orthorhombic (2) max(rcut+rcut_buffer) < min(box)/2
rcut_buffer      = 0.6                 # buffer radius (Angstrom) of neighborlist (can be a list for different types)
fix_type         = 'NVT'               # 'NVE' 'NVT'
chain_length     = 1                   # Nose-Hoover chain length in NVT
tau              = 1000 * dt           # Nose-Hoover relaxation time in NVT

# Prepare initial config numpy.array however you like; coord (N,3); box: (3,3) or (3,)(orthorhombic) or scalar (cubic)
# Note: please group the atoms by type and make sure types are always in the same order as in training the model
type_idx         = [0, 128, 384]       # indices of type i atom in [type_idx[i], type_idx[i+1])
sanity_check     = True                # Check model error; if True, please provide true force (N,3) for initial config
# Here as an example we use a configuration from the training dataset and repeat it to a larger box
path = '/pscratch/sd/r/ruiqig/polaron_cp2k/aimd/aimd-water/water_128/'
coord = np.load(path + 'set.001/coord.npy')[0].reshape(-1, 3)
force = np.load(path + 'set.001/force.npy')[0].reshape(-1, 3)
box = np.load(path + 'set.001/box.npy')[0].reshape(3, 3)
type_list = np.genfromtxt(path + 'type.raw').astype(int)
coord = np.concatenate([coord[type_list==i] for i in range(len(type_idx) - 1)])
force = np.concatenate([force[type_list==i] for i in range(len(type_idx) - 1)])
repeat = [1,1,1] # number of repeats in each direction x,y,z
for k in range(3):
    coord = np.concatenate([(coord + i*box[k])[:,None] for i in range(repeat[k])], axis=1).reshape(-1,3)
force = np.repeat(force, np.prod(repeat), axis=0)
box = np.diag(box) * np.array(repeat)
type_idx = np.array(type_idx) * np.prod(repeat) # upscale type_idx accordingly
# end preparing initial config

# Prepare energy function for simulation; no need to change anything
mass = np.concatenate([mass[i] * np.ones(type_idx[i+1]-type_idx[i], dtype=np.float32) for i in range(len(type_idx)-1)])
if np.array(box).size == 1:
    box = box * np.eye(3)
if np.array(box).size == 3:
    box = np.diag(box)
with open(model_path, 'rb') as f:
    m = pickle.load(f)
model, variables = m['model'], jax.device_put(m['variables'], shard_all)
model_list, variables_list = [model], [variables]
if use_model_devi:
    for path in model_devi_paths:
        with open(path, 'rb') as f:
            m = pickle.load(f)
            model_list.append(m['model']), variables_list.append(jax.device_put(m['variables'],shard_all))
rcut_max = max([m.params['rcut'] for m in model_list])
lattice_args = compute_lattice_candidate(box[None], rcut_max, print_message=True)
static_args = nn.FrozenDict({'type_idx':type_idx, 'lattice':lattice_args, 'K':jax.device_count()})
coord, box = jax.device_put(coord, shard_all), jax.device_put(box, shard_all)
displace, shift = space.periodic(np.diag(box) if lattice_args['ortho'] else box)
nbrs_list = None
if use_neighborlist:
    update_every, buffer_size = 5, 1.2
    rcut_buffer = np.ones(len(type_idx)-1)*rcut_buffer if np.array(rcut_buffer).size == 1 else np.array(rcut_buffer)
    rbuffer_array = jnp.concatenate([np.ones(type_idx[i+1]-type_idx[i],dtype=np.float32)*rcut_buffer[i] for i in range(len(rcut_buffer))])
    neighborlist = NeighborListLoader(np.diag(box), type_idx, rcut_max + rcut_buffer, buffer_size, jax.device_count())
    nbrs_list = neighborlist.allocate(coord % jnp.diag(box))
def get_energy_fn(model, variables):
    def energy_fn(coord, nbrs_list):
        K = jax.device_count() if use_neighborlist else 1
        coord = jax.device_put(reorder_by_device(coord, tuple(type_idx), K), shard_all)
        return model.apply(variables, coord.T, box.T, static_args, nbrs_list)[0]
    return jit(energy_fn)
energy_fn = get_energy_fn(model, variables) # for simulation
energy_fns = [get_energy_fn(model, variables) for model, variables in zip(model_list, variables_list)] # for model deviation
@jit
def compute_model_devi(coord, nbrs_list):
    all_forces = jnp.array([-grad(energy_fn)(coord, nbrs_list) for energy_fn in energy_fns])
    return jnp.std(all_forces, axis=0).max()
if sanity_check:
    print('# Sanity check: NAtoms = ', len(coord), 'Energy = ', energy_fn(coord, nbrs_list),
        'Force error = ', ((force + jit(grad(energy_fn))(coord, nbrs_list))**2).mean()**0.5)
    
# Begin simulation
TIC = time()
if fix_type == 'NVT':
    init_fn, apply_fn = simulate.nvt_nose_hoover(energy_fn, shift, dt, temp, chain_length=chain_length, tau=tau) 
    state = init_fn(random.PRNGKey(0), coord, mass=mass, nbrs_list=nbrs_list)
elif fix_type == 'NVE':                    
    init_fn, apply_fn = simulate.nve(energy_fn, shift, dt)                               
    state = init_fn(random.PRNGKey(0), coord, mass=mass, kT=temp, nbrs_list=nbrs_list)
state_shard, nbrs_shard = jax.tree_util.tree_map(lambda x: x.sharding, state), jax.tree_util.tree_map(lambda x: x.sharding, nbrs_list)
def step_fn(states, i):
    state, nbrs_list = states
    state = apply_fn(state, nbrs_list=nbrs_list)
    return (state, nbrs_list), (state.position, state.velocity)
def get_multi_step_fn(steps):
    def multi_step_fn(states, i):
        state, nbrs_list = states
        nbrs_list = neighborlist.update(state.position % jnp.diag(box), nbrs_list)
        (state_new, _), (pos, vel) = jax.lax.scan(step_fn, (state,nbrs_list), None, steps)
        rcut_overflow = (jnp.linalg.norm((state.position-pos-jnp.diag(box)/2)%jnp.diag(box)-jnp.diag(box)/2, axis=-1) > rbuffer_array/2).any()
        return (state_new, nbrs_list), (pos, vel, rcut_overflow)
    return multi_step_fn
multi_step_fn = get_multi_step_fn(update_every) if use_neighborlist else None
print('# Step\tTemp\tKE\tPE\tInvariant\tModel Dev\ttime')
print('################################################')
pos_traj, vel_traj, model_devi_traj = [], [], []
i, tic, NBRS_FLAG = 0, time(), False
while i < total_steps:
    if i % print_every < (update_every if use_neighborlist else 1):
        PE = energy_fn(state.position, nbrs_list=nbrs_list)
        T = quantity.temperature(velocity=state.velocity, mass=state.mass) / 1.380649e-23 * 1.602176634e-19
        KE = quantity.kinetic_energy(velocity=state.velocity, mass=state.mass)
        if fix_type == 'NVT':
            inv = simulate.nvt_nose_hoover_invariant(energy_fn, state, temp, nbrs_list=nbrs_list)
        elif fix_type == 'NVE':
            inv = PE + KE
        model_devi = compute_model_devi(state.position, nbrs_list) if use_model_devi else 0.
        print('{}\t{:.2f}\t{:.2f}\t{:.2f}\t{:.2f}\t{:.3f}\t{:.2f}'.format(
                i, T, KE, PE, inv, model_devi, (time() - tic)))
        tic = time()
    if not use_neighborlist:
        (state, _), (pos, vel) = jax.lax.scan(step_fn, (state,None), None, print_every)
        i += print_every
    else:
        state, nbrs_list = jax.device_put(state, state_shard), jax.device_put(nbrs_list, nbrs_shard)
        (state_new, nbrs_list_new), (pos, vel, rcut_overflow) = jax.lax.scan(multi_step_fn, (state,nbrs_list), None, print_every//update_every)
        if any([nbrs.did_buffer_overflow for nbrs in nbrs_list_new]):
            if NBRS_FLAG:
                NBRS_FLAG, buffer_size = False, buffer_size + 0.05
                print('# Neighbor list overflow for a second time; Increasing buffer_size to', buffer_size)
                NeighborListLoader(np.diag(box), type_idx, rcut_max + np.array(rcut_buffer), buffer_size, jax.device_count())
            else:
                NBRS_FLAG = True
            nbrs_list = neighborlist.allocate(state.position % jnp.diag(box))
            nbrs_shard = jax.tree_util.tree_map(lambda x: x.sharding, nbrs_list)
            continue
        NBRS_FLAG = False
        if rcut_overflow.any():
            if update_every == 1:
                print('# Error: rcut_buffer overflow for a single step; Check for bugs or increase rcut_buffer')
                break
            else:
                update_every = max(update_every//2, 1)
                multi_step_fn = get_multi_step_fn(update_every)
                print('# rcut_buffer overflow; Decreasing update_every to', update_every)
                continue
        state, nbrs_list = state_new, nbrs_list_new
        i += update_every * (print_every // update_every)
        pos, vel = pos.reshape(-1,pos.shape[2],3), vel.reshape(-1,vel.shape[2],3)
    pos_traj.append(np.array(pos[save_every-1::save_every]))
    vel_traj.append(np.array(pos[save_every-1::save_every]))
    model_devi_traj.append(model_devi)
pos_traj, vel_traj, model_devi_traj = np.concatenate(pos_traj), np.concatenate(vel_traj), np.array(model_devi_traj)
np.save(save_path + '/' + save_prefix + '_pos.npy', pos_traj)
np.save(save_path + '/' + save_prefix + '_vel.npy', vel_traj)
print('# Trajectory saved to \'%s_*\'.' % os.path.realpath(save_path+'/'+save_prefix))
Time = time() - TIC
print('# Finished simulation in %dh %dm %ds.' % (Time//3600,(Time%3600)//60,Time%60))