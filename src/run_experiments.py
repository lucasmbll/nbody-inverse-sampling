# run_experiment.py

import os
#os.environ["XLA_PYTHON_CLIENT_MEM_FRACTION"] = "1.0" # Set memory fraction for JAX

import yaml
import datetime 
import argparse
import shutil
import numpy as np
from typing import List
from mpi4py import MPI

# GPU configuration 
def check_mpi_mode():
    comm = MPI.COMM_WORLD
    if comm.Get_size() > 1:
        return True, comm, comm.Get_rank(), comm.Get_size()
    return False, 0, 1, None

def setup_mpi_gpu_assignment(gpu_devices: List[int], rank: int):
    assigned_gpu = gpu_devices[rank]
    print(f"Process {rank} assigned GPU {assigned_gpu} from list {gpu_devices}")
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ['CUDA_VISIBLE_DEVICES'] = str(assigned_gpu)
    return assigned_gpu

def gpu_config(config):
    use_mpi, comm, rank, size = check_mpi_mode()
    if use_mpi and config.get("num_chains", 1) > 1:
        gpu_devices = config.get("mpi_gpu_devices", None)
        if gpu_devices is not None:
            setup_mpi_gpu_assignment(gpu_devices, rank)
        else:
            raise ValueError("MPI mode requires 'mpi_gpu_devices' to be specified in the configuration file.")
    else:
        cuda_num = config.get("cuda_visible_devices", None)
        if cuda_num is not None:
            os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            os.environ["CUDA_VISIBLE_DEVICES"] = str(cuda_num)
            print(f"CUDA device set to: {cuda_num}")
    return use_mpi, rank, size, comm
        
# Main function to run the experiment
def main(config_path):

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    
    use_mpi, rank, size, comm = gpu_config(config)

    import jax
    import jax.numpy as jnp
    from model import model 

    mode = config.get("mode")
    if mode is None:
        raise ValueError("Please specify the mode in the configuration file under 'mode' key : 'sim' or 'sampling'.")
    if mode not in ["sim", "sampling"]:
        raise ValueError(f"Invalid mode: {mode}. Must be either 'sim' or 'sampling'")

    # --- Output directory ---
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    config_base = os.path.splitext(os.path.basename(config_path))[0]
    if mode == "sampling":
        base_dir = os.path.join("results", "sampling_results", f"{config_base}_{now_str}")
    else:  # mode == "sim"
        base_dir = os.path.join("results", "nbody_sim_results", f"{config_base}_{now_str}")
    os.makedirs(base_dir, exist_ok=True)

    # --- Model parameters ---
    model_params = config["model_params"]
    model_fn = model 

    G = model_params.get("G", 5.0)
    length = model_params.get("length", 64)
    softening = model_params.get("softening", 0.1)
    t_f = model_params.get("t_f", 1.0)
    dt = model_params.get("dt", 0.5)
    m_part = model_params.get("m_part", 1.0)
    solver = model_params.get("solver", "LeapfrogMidpoint") 

    data_seed = model_params.get("data_seed", 0) # Random seed for data generation
    data_key = jax.random.PRNGKey(data_seed)
    
    density_scaling = model_params.get("density_scaling", "none")
    scaling_kwargs = model_params.get("scaling_kwargs", {})
    
    blobs_params = model_params.get("blobs_params", [])
    if not blobs_params:
        raise ValueError("Blob parameters 'blobs_params' must be specified in the configuration file.")
    
    n_part = sum(blob['n_part'] for blob in blobs_params)
    total_mass = sum(blob['n_part'] * blob.get('m_part', 1.0) for blob in blobs_params)

    # Run model for simulation (sim mode) or mock data generation (sampling mode)
    if not use_mpi or rank == 0:  # Only one process runs the model 
        result = model_fn(
            blobs_params,
            G=G,
            length=length,
            softening=softening,
            t_f=t_f,
            dt=dt,
            key=data_key,
            density_scaling=density_scaling,
            solver=solver,
            **scaling_kwargs
        )
        
        input_field, output_field, sol_ts, sol_ys, masses = result

        if mode == "sim":
            print(f"Simulation completed.")
            print(f"Total particles: {n_part}, Total mass: {total_mass:.2f}")
        else:
            print(f"Mock data generated.")
            print(f"Total particles: {n_part}, Total mass: {total_mass:.2f}") 
    
    if use_mpi:  # If using MPI, we need to broadcast the data to all processes
        comm = MPI.COMM_WORLD
        
        if rank == 0:
            # Root process prepares data for broadcasting
            shared_data = {
                'input_field': np.array(input_field),
                'output_field': np.array(output_field),
                'sol_ts': np.array(sol_ts),
                'sol_ys': np.array(sol_ys),
                'masses': np.array(masses)
            }
        else:
            shared_data = None
        
        # Broadcast to all processes
        shared_data = comm.bcast(shared_data, root=0)
        
        # Convert back to JAX arrays on non-root processes
        if rank != 0:
            input_field = jnp.array(shared_data['input_field'])
            output_field = jnp.array(shared_data['output_field'])
            masses = jnp.array(shared_data['masses'])
            sol_ys = jnp.array(shared_data['sol_ys'])
            sol_ts = jnp.array(shared_data['sol_ts'])

    data = output_field  # This is the mock data we will use for inference (sampling mode)

    # --- Plots and video ---
    plot_settings = config.get("plot_settings", {})
    
    # Pre-compute energy if needed for any plots
    enable_energy_tracking = plot_settings.get("enable_energy_tracking", True)
    energy_data = None

    if enable_energy_tracking:
            print("Pre-calculating energy for all timesteps...")
            from utils import calculate_energy_variable_mass
            all_times = []
            all_ke = []
            all_pe = []
            all_te = []
            
            for i in range(len(sol_ts)):
                pos_t = sol_ys[i, 0]
                vel_t = sol_ys[i, 1]
                ke, pe, te = calculate_energy_variable_mass(pos_t, vel_t, masses, G, length, softening)
                all_times.append(sol_ts[i])
                all_ke.append(ke)
                all_pe.append(pe)
                all_te.append(te)
            
            energy_data = {
                'times': jnp.array(all_times),
                'kinetic': jnp.array(all_ke),
                'potential': jnp.array(all_pe),
                'total': jnp.array(all_te)
            }
            print("Energy calculation completed.")
    
    if plot_settings['density_field_plot'].get("do"):
        from plotting import plot_density_fields_and_positions, plot_position_vs_radius_blobs
        print("Creating density fields and positions plot...")
        fig = plot_density_fields_and_positions(
            G, t_f, dt, length, n_part, input_field, sol_ys[0, 0], sol_ys[-1, 0], output_field, 
            density_scaling=density_scaling, solver=solver,)
        fig.savefig(os.path.join(base_dir, "density_fields_and_positions.png"))
        fig2 = plot_position_vs_radius_blobs(sol_ts, sol_ys, blobs_params, length, time_idx=0)
        fig2.savefig(os.path.join(base_dir, "position_vs_radius_blobs.png"))
        print("Density fields and positions plots saved successfully")
    
    if plot_settings['timeseries_plot'].get("do"):
        from plotting import plot_timesteps
        print("Creating timesteps plot...")
        plot_timesteps_num = config.get("plot_timesteps", 10)
        fig, _ = plot_timesteps(sol_ts, sol_ys, length, G, t_f, dt, n_part, num_timesteps=plot_timesteps_num, softening=softening, masses=masses, solver=solver,
                                enable_energy_tracking=enable_energy_tracking, density_scaling=density_scaling,
                                energy_data=energy_data)
        fig.savefig(os.path.join(base_dir, "timesteps.png"))
        print("Timesteps plot saved successfully")
    
    if plot_settings['trajectories_plot'].get("do"):
        from plotting import plot_trajectories
        print("Creating trajectories plot...")
        num_trajectories = plot_settings['trajectories_plot'].get("num_trajectories", 10)
        zoom = plot_settings['trajectories_plot'].get("zoom_for_trajectories", True)
        fig = plot_trajectories(sol_ys, G, t_f, dt, length, n_part, solver, num_trajectories=num_trajectories, 
                                zoom=zoom)
        fig.savefig(os.path.join(base_dir, "trajectories.png"))
        print("Trajectories plot saved successfully")
    
    if plot_settings['velocity_plot'].get("do"):
        from plotting import plot_velocity_distributions, plot_velocity_vs_radius_blobs
        print("Creating velocity distributions plot...")
        fig, _ = plot_velocity_distributions(sol_ys, G, t_f, dt, length, n_part, solver)
        fig.savefig(os.path.join(base_dir, "velocity_distributions.png"))
        fig2 = plot_velocity_vs_radius_blobs(sol_ts, sol_ys, blobs_params, G, masses, softening)
        fig2.savefig(os.path.join(base_dir, "velocity_wrt_radius.png"))
        print("Velocity distributions plot saved successfully")
 
    if plot_settings['generate_video'].get("do"): # need to be corrected
        print("Creating simulation video...")
        from plotting import create_video
        video_path = os.path.join(base_dir, "simulation_video.mp4")
        fps = plot_settings['generate_video'].get("video_fps", 10)
        dpi = plot_settings['generate_video'].get("video_dpi", 100)
        
        create_video(sol, length, G, t_f, dt, n_part, 
                    save_path=video_path, fps=fps, dpi=dpi, density_scaling=density_scaling, solver=solver,
                    softening=softening, masses=masses,
                    enable_energy_tracking=enable_energy_tracking,
                    energy_data=energy_data)
        print("Simulation video saved successfully")
        
    shutil.copy(config_path, os.path.join(base_dir, "config.yaml")) # Save a copy of the config file in the result directory
    
    if mode == "sim":
        print("Simulation completed.")
        return

    # --- SAMPLING ---
    from likelihood import get_log_posterior
    #from sampling import extract_params_to_infer
    from utils import extract_true_values_from_blobs

    print('Starting sampling process...')
    
    # Prior
    prior_type = config.get("prior_type", None)
    prior_params = config.get("prior_params", None)
    if prior_params is None or prior_type is None:
        raise ValueError("No prior specified in config file. Please provide 'prior_params' and 'prior_type' in your configuration.")
    
    # Likelihood
    likelihood_type = config.get("likelihood_type", None)
    likelihood_kwargs = config.get("likelihood_kwargs", {})
    if likelihood_type is None:
        raise ValueError("No likelihood type specified in config file. Please provide 'likelihood_type' in your configuration.")
    model_kwargs = {
        "G": G,
        "length": length,
        "softening": softening,
        "t_f": t_f,
        "dt": dt,
        "m_part": m_part,
        "density_scaling": density_scaling,
        "solver": solver,
        **scaling_kwargs
    }
    
    # Posterior
    log_posterior = get_log_posterior(
        likelihood_type, 
        data, 
        prior_params=prior_params,
        prior_type=prior_type,
        model_fn=model_fn,
        init_params=blobs_params,
        **model_kwargs,
        **likelihood_kwargs
    )

    # Sampling initialization
    num_chains = config.get("num_chains", 1)
    initial_position = config.get("initial_position", None)
    if initial_position is None:
        raise ValueError("No initial position specified in config file. Please provide 'initial_position' in your configuration.")
    rng_key = jax.random.PRNGKey(config.get("sampling_seed", 12345))
    num_samples = config.get("num_samples", 1000)

    # Run the sampler
    if use_mpi and num_chains > 1:
        from sampling_mpi import run_mpi_sampling
        samples, rhat_values = run_mpi_sampling(
            config["sampler"], log_posterior, rank, size, comm, config, base_dir, rng_key
        )
        
        # Only root process continues with post-processing
        if rank != 0:
            return
            
        # Restore stdout for root process
        import sys
        sys.stdout = sys.__stdout__
        
        # samples is already in the right format for plotting
        samples_dict = samples
        
        # Load chain-separated samples for trace plots
        chain_samples_file = os.path.join(base_dir, "samples_by_chain.npz")
        chain_samples = None
        if os.path.exists(chain_samples_file):
            chain_data = np.load(chain_samples_file)
            chain_samples = {key: chain_data[key] for key in chain_data.files}
            print(f"Loaded chain-separated samples: {list(chain_samples.keys())}")
        
    else:
        # Single-process execution or single chain
        chain_samples = None  # No chain separation for single process
        
        if config["sampler"] == "hmc":
            from sampling import run_hmc
            print("Running HMC sampler...")
            inv_mass_matrix = np.array(config["inv_mass_matrix"])
            step_size = config.get("step_size", 1e-3)
            num_integration_steps = config.get("num_integration_steps", 50)
            num_warmup = config.get("num_warmup", 1000)
            samples = run_hmc(
                log_posterior,
                initial_position,
                inv_mass_matrix,
                step_size,
                num_integration_steps,
                rng_key,
                num_samples,
                num_warmup
            )

        elif config["sampler"] == "nuts":
            print("Running NUTS sampler...")
            from sampling import run_nuts
            num_warmup = config.get("num_warmup", 1000)
            samples = run_nuts(
                log_posterior,
                initial_position,
                rng_key,
                num_samples,
                num_warmup
            )

        elif config["sampler"] == "rwm":
            print("Running Random Walk Metropolis sampler...")
            from sampling import run_rwm
            step_size = config.get("step_size", 0.1)
            samples = run_rwm(
                log_posterior,
                initial_position,
                step_size,
                rng_key,
                num_samples
            )

        elif config["sampler"] == "mala":
            print("Running MALA sampler...")
            from sampling import run_mala
            step_size = config.get("step_size", 0.01)
            num_warmup = config.get("num_warmup", 0)  
            autotuning = config.get("autotuning", False)
            samples = run_mala(
                log_posterior,
                initial_position,
                step_size,
                rng_key,
                num_samples,
                num_warmup=num_warmup,  
                autotuning=autotuning
            )

        else:
            raise ValueError("Unknown sampler: should be 'hmc', 'nuts', 'rwm', or 'mala'")
        
        samples_dict = {}
        for key, value in samples.items():
            if isinstance(value, list) and all(hasattr(v, 'ndim') for v in value):
                # Stack list of arrays into a single 2D array
                samples_dict[key] = jnp.stack(value, axis=1)
            else:
                # Keep as is for scalar parameters
                samples_dict[key] = value
    
    print("Sampling finished.")
    
    np.savez(os.path.join(base_dir, "samples.npz"), **{k: np.array(v) for k, v in samples_dict.items()})
    print(f"Results saved to {os.path.join(base_dir, 'samples.npz')}")   

    inferred_keys = list(samples_dict.keys())

    theta = {}
    all_true_values = extract_true_values_from_blobs(blobs_params)
    # Build theta dict for only the parameters that were actually sampled
    for key in inferred_keys:
        if key in all_true_values:
            theta[key] = all_true_values[key]
        else:
            theta[key] = None
            print(f"⚠️  Warning: No true value found for '{key}' in blobs_params.")

    param_order = tuple(inferred_keys)

    # Sampling plots
    from plotting import plot_trace_subplots, plot_corner_after_burnin
    from utils import format_prior_info, format_initial_pos

    # Build info strings for each parameter
    param_info = {}
    for param_name in inferred_keys:
        prior_str = format_prior_info(param_name, prior_params, prior_type)
        init_str = format_initial_pos(param_name, initial_position)
        param_info[param_name] = f"{prior_str} | {init_str}"

    # Create a list of strings for the suptitle
    info_lines = [f"{param}: {info}" for param, info in param_info.items()]
    max_per_line = 1
    lines = [" | ".join(info_lines[i:i+max_per_line]) for i in range(0, len(info_lines), max_per_line)]

    fig, _ = plot_trace_subplots(
        samples_dict,
        theta=theta,
        G=G, t_f=t_f, dt=dt, softening=softening, length=length, n_part=n_part,
        method=config["sampler"],
        param_order=param_order,
        chain_samples=chain_samples  # Pass chain samples for individual trace plots
    )
    existing_suptitle = fig._suptitle.get_text() if fig._suptitle else "Trace Plot"
    new_suptitle = existing_suptitle + "\n" + "\n".join(lines)
    fig.suptitle(new_suptitle, fontsize=10)
    fig.savefig(os.path.join(base_dir, "trace_sampling.png"))
    print("Trace plot saved.")

    burnin = num_warmup if config["sampler"] in ["nuts", "hmc", "mala"] else 0
    fig = plot_corner_after_burnin(
        samples_dict,
        theta=theta,
        G=G, t_f=t_f, dt=dt, softening=softening, length=length, n_part=n_part,
        method=config["sampler"],
        param_order=param_order,
        burnin=burnin,
        chain_samples=chain_samples  # Pass for title information only
    )
    existing_suptitle = fig._suptitle.get_text() if fig._suptitle else "Corner Plot"
    new_suptitle = existing_suptitle + "\n" + "\n".join(lines)
    fig.suptitle(new_suptitle, fontsize=10)
    fig.savefig(os.path.join(base_dir, "corner_sampling.png"))
    print("Corner plot saved.")

    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run MCMC sampling for N-body initial distribution parameters inference")
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config file")
    args = parser.parse_args()
    main(args.config)