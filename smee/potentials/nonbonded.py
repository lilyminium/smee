"""Non-bonded potential energy functions."""
import collections
import math

import openff.units
import torch

import smee.potentials
import smee.utils

_UNIT = openff.units.unit

_COULOMB_PRE_FACTOR_UNITS = (
    _UNIT.kilocalorie / _UNIT.mole * _UNIT.angstrom / _UNIT.e**2
)
_COULOMB_PRE_FACTOR = (_UNIT.avogadro_constant / (4.0 * _UNIT.pi * _UNIT.eps0)).m_as(
    _COULOMB_PRE_FACTOR_UNITS
)
_COULOMB_POTENTIAL = "coul"

_PME_MIN_NODES = torch.tensor(6)  # taken to match OpenMM 8.0.0
_PME_ORDER = 5  # see OpenMM issue #2567

_LJ_POTENTIAL = "4*epsilon*((sigma/r)**12-(sigma/r)**6)"


def lorentz_berthelot(
    epsilon_a: torch.Tensor,
    epsilon_b: torch.Tensor,
    sigma_a: torch.Tensor,
    sigma_b: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Computes the Lorentz-Berthelot combination rules for the given parameters.

    Args:
        epsilon_a: The epsilon [kcal / mol] values of the first particle in each pair
            with ``shape=(n_pairs, 1)``.
        epsilon_b: The epsilon [kcal / mol] values of the second particle in each pair
            with ``shape=(n_pairs, 1)``.
        sigma_a: The sigma [kcal / mol] values of the first particle in each pair
            with ``shape=(n_pairs, 1)``.
        sigma_b: The sigma [kcal / mol] values of the second particle in each pair
            with ``shape=(n_pairs, 1)``.

    Returns:
        The epsilon [kcal / mol] and sigma [Å] values of each pair, each with
        ``shape=(n_pairs, 1)``.
    """
    return (epsilon_a * epsilon_b).sqrt(), 0.5 * (sigma_a + sigma_b)


def _compute_pairwise(
    conformer: torch.Tensor, box_vectors: torch.Tensor, cutoff: torch.Tensor
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    import NNPOps.neighbors

    (
        pairs,
        deltas,
        distances,
        _,
    ) = NNPOps.neighbors.getNeighborPairs(conformer, cutoff.item(), -1, box_vectors)

    are_interacting = ~torch.isnan(distances)

    pairs, _ = pairs[:, are_interacting].sort(dim=0)
    distances = distances[are_interacting]
    deltas = deltas[are_interacting, :]

    return pairs, deltas, distances


def broadcast_exclusions(
    system: smee.TensorSystem, potential: smee.TensorPotential
) -> tuple[torch.Tensor, torch.Tensor]:
    """Broadcasts the exclusions (indices and scale factors) of each topology to the
    full system.

    Args:
        system: The system.
        potential: The potential containing the scale factors to broadcast.

    Returns:
        The exception indices with shape ``(n_exceptions, 2)`` and the scale factors
        with shape ``(n_exceptions,)``.
    """

    idx_offset = 0

    per_topology_exclusion_idxs = []
    per_topology_exclusion_scales = []

    for topology, n_copies in zip(system.topologies, system.n_copies):
        exclusion_offset = idx_offset + torch.arange(n_copies) * topology.n_particles

        exclusion_idxs = topology.parameters[potential.type].exclusions
        exclusion_idxs = exclusion_offset[:, None, None] + exclusion_idxs[None, :, :]

        exclusion_scales = potential.attributes[
            topology.parameters[potential.type].exclusion_scale_idxs
        ]
        exclusion_scales = torch.broadcast_to(
            exclusion_scales, (n_copies, *exclusion_scales.shape)
        )

        per_topology_exclusion_idxs.append(exclusion_idxs.reshape(-1, 2))
        per_topology_exclusion_scales.append(exclusion_scales.reshape(-1))

        idx_offset += n_copies * topology.n_particles

    return (
        torch.vstack(per_topology_exclusion_idxs),
        torch.cat(per_topology_exclusion_scales),
    )


def _compute_pair_scales(
    system: smee.TensorSystem, potential: smee.TensorPotential
) -> torch.Tensor:
    """Returns the scale factor for each interaction in the full system by broadcasting
    and stacking the exclusions of each topology.

    Args:
        system: The system.
        potential: The potential containing the scale factors to broadcast.

    Returns:
        The parameters for the full system with
        ``shape=(n_particles * (n_particles - 1) / 2,)``.
    """

    n_particles = system.n_particles
    n_pairs = (n_particles * (n_particles - 1)) // 2

    exclusion_idxs, exclusion_scales = broadcast_exclusions(system, potential)

    pair_scales = smee.utils.ones_like(n_pairs, other=potential.parameters)

    if len(exclusion_idxs) > 0:
        exclusion_idxs, _ = exclusion_idxs.sort(dim=1)  # ensure upper triangle

        pair_idxs = smee.utils.to_upper_tri_idx(
            exclusion_idxs[:, 0], exclusion_idxs[:, 1], n_particles
        )
        pair_scales[pair_idxs] = exclusion_scales

    return pair_scales


def _compute_dispersion_integral(
    r: torch.Tensor, rs: torch.Tensor, rc: torch.Tensor, sigma: torch.Tensor
) -> torch.Tensor:
    """Evaluate the integral needed to compute the LJ long range dispersion correction
    due to the switching function.

    Notes:
        The math was very gratefully copied from OpenMM: https://github.com/openmm/openmm/blob/0363c38dc7ba5abc40d5d4c72efbca0718ff09ab/openmmapi/src/NonbondedForceImpl.cpp#L234C32-L234C32
        See LICENSE_3RD_PARTY for the OpenMM license and copyright notice.

    Args:
        r: The distance to evaluate the integral at.
        rs: The switching distance.
        rc: The cutoff distance.
        sigma: The sigma value of the pair.

    Returns:
        The evaluated integral.
    """
    A = 1 / (rc - rs)
    A2 = A * A
    A3 = A2 * A
    sig2 = sigma * sigma
    sig6 = sig2 * sig2 * sig2
    rs2 = rs * rs
    rs3 = rs * rs2
    r2 = r * r
    r3 = r * r2
    r4 = r * r3
    r5 = r * r4
    r6 = r * r5
    r9 = r3 * r6
    # fmt: off
    return (
        sig6 * A3 * ((
            sig6 * (
                + rs3 * 28 * (6 * rs2 * A2 + 15 * rs * A + 10)
                - r * rs2 * 945 * (rs2 * A2 + 2 * rs * A + 1)
                + r2 * rs * 1080 * (2 * rs2 * A2 + 3 * rs * A + 1)
                - r3 * 420 * (6 * rs2 * A2 + 6 * rs * A + 1)
                + r4 * 756 * (2 * rs * A2 + A)
                - r5 * 378 * A2)
            - r6 * (
                +rs3 * 84 * (6 * rs2 * A2 + 15 * rs * A + 10)
                - r * rs2 * 3780 * (rs2 * A2 + 2 * rs * A + 1)
                + r2 * rs * 7560 * (2 * rs2 * A2 + 3 * rs * A + 1)))
            / (252 * r9)
            - torch.log(r) * 10 * (6 * rs2 * A2 + 6 * rs * A + 1)
            + r * 15 * (2 * rs * A2 + A)
            - r2 * 3 * A2)
    )
    # fmt: on


def _compute_dispersion_term(
    count: float,
    epsilon: torch.Tensor,
    sigma: torch.Tensor,
    cutoff: torch.Tensor | None,
    switch_width: torch.Tensor | None,
) -> torch.Tensor:
    """Computes the terms of the LJ dispersion correction for a particular type of
    interactions (i.e., ii and ij).

    Args:
        count: The number of interactions of this type with ``shape=(n_parameters,)``.
        epsilon: The epsilon values of each interaction with ``shape=(n_parameters,)``.
        sigma: The sigma values of each interaction with ``shape=(n_parameters,)``.
        cutoff: The cutoff distance.
        switch_width: The distance at which the switching function begins to apply.

    """
    sigma6 = sigma**6

    terms = [sigma6 * sigma6, sigma6]

    if switch_width is not None:
        assert cutoff is not None

        terms.append(
            _compute_dispersion_integral(cutoff, switch_width, cutoff, sigma)
            - _compute_dispersion_integral(switch_width, switch_width, cutoff, sigma)
        )

    return (count * epsilon * torch.stack(terms)).sum(dim=-1)


def _compute_dispersion_correction(
    system: smee.TensorSystem,
    potential: smee.TensorPotential,
    cutoff: torch.Tensor | None,
    switch_width: torch.Tensor | None,
    volume: torch.Tensor,
) -> torch.Tensor:
    """Computes the long range dispersion correction due to the switching function.

    Args:
        system: The system to compute the correction for.
        potential: The LJ potential.
        cutoff: The cutoff distance.
        switch_width: The distance at which the switching function begins to apply.
        volume: The volume of the system.

    Returns:

    """
    n_by_type = collections.defaultdict(int)

    for topology, n_copies in zip(system.topologies, system.n_copies):
        parameter_counts = topology.parameters["vdW"].assignment_matrix.abs().sum(dim=0)

        for key, count in zip(potential.parameter_keys, parameter_counts):
            n_by_type[key] += count.item() * n_copies

    counts = torch.tensor(
        [n_by_type[key] for key in potential.parameter_keys], dtype=torch.float32
    )

    # particles of the same type interacting
    n_ii_interactions = (counts * (counts + 1.0)) / 2.0

    eps_ii, sig_ii = potential.parameters[:, 0], potential.parameters[:, 1]

    terms = _compute_dispersion_term(
        n_ii_interactions, eps_ii, sig_ii, cutoff, switch_width
    )

    # particles of different types interacting
    idx_i, idx_j = torch.triu_indices(len(counts), len(counts), 1)
    n_ij_interactions = counts[idx_i] * counts[idx_j]

    eps_ij, sig_ij = lorentz_berthelot(
        eps_ii[idx_i], eps_ii[idx_j], sig_ii[idx_i], sig_ii[idx_j]
    )
    terms += _compute_dispersion_term(
        n_ij_interactions, eps_ij, sig_ij, cutoff, switch_width
    )

    n_particles = system.n_particles
    n_interactions = (n_particles * (n_particles + 1)) / 2

    terms /= n_interactions

    return (
        8.0
        * n_particles**2
        * torch.pi
        * (terms[0] / (9 * cutoff**9) - terms[1] / (3 * cutoff**3) + terms[2])
        / volume
    )


def _compute_lj_energy_periodic(
    system: smee.TensorSystem,
    conformer: torch.Tensor,
    box_vectors: torch.Tensor,
    potential: smee.TensorPotential,
) -> torch.Tensor:
    """Compute the potential energy [kcal / mol] of a periodic system due to
    LJ interactions.

    Args:
        system: The system to compute the energy for.
        conformer: The conformer [Å] to evaluate the potential at with
            ``shape=(n_particles, 3)``.
        box_vectors: The box vectors [Å] of the system. with ``shape=(3, 3)``.
        potential: The LJ potential.

    Returns:
        The potential energy [kcal / mol].
    """
    assert system.is_periodic, "the system must be periodic."

    volume = torch.det(box_vectors)

    parameters = smee.potentials.broadcast_parameters(system, potential)
    pair_scales = _compute_pair_scales(system, potential)

    cutoff = potential.attributes[potential.attribute_cols.index("cutoff")]

    pairs, _, distances = _compute_pairwise(conformer, box_vectors, cutoff)

    pair_idxs = smee.utils.to_upper_tri_idx(pairs[0, :], pairs[1, :], len(parameters))
    pair_scales = pair_scales[pair_idxs]

    epsilon, sigma = lorentz_berthelot(
        parameters[pairs[0, :], 0],
        parameters[pairs[1, :], 0],
        parameters[pairs[0, :], 1],
        parameters[pairs[1, :], 1],
    )

    use_switch_fn = "switch_width" in potential.attribute_cols

    switch_width_idx = (
        None if not use_switch_fn else potential.attribute_cols.index("switch_width")
    )
    switch_width = (
        None if not use_switch_fn else (cutoff - potential.attributes[switch_width_idx])
    )
    switch_fn = 1.0

    if use_switch_fn:
        x_switch = (distances - switch_width) / (cutoff - switch_width)

        switch_fn = (
            1.0 - 6.0 * x_switch**5 + 15.0 * x_switch**4 - 10.0 * x_switch**3
        )
        switch_fn = torch.where(distances < switch_width, torch.tensor(1.0), switch_fn)
        switch_fn = torch.where(distances > cutoff, torch.tensor(0.0), switch_fn)

    x = (sigma / distances) ** 6

    energy = (switch_fn * pair_scales * 4.0 * epsilon * (x * (x - 1.0))).sum(-1)
    energy += _compute_dispersion_correction(
        system, potential, switch_width, cutoff, volume
    )

    return energy


def compute_pairwise(
    conformer: torch.Tensor,
    exclusions: torch.Tensor,
    exclusion_scales: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Computes the pairwise squared distance between all pairs of particles, and the
    corresponding scale factor based off any exclusions.

    Args:
        conformer: The conformer(s).
        exclusions: A tensor containing pairs of atom indices whose interaction should
            be scaled by ``exclusion_scales`` with ``shape=(n_exclusions, 2)``.
        exclusion_scales: A tensor containing the scale factor for each exclusion pair
            with ``shape=(n_exclusions, 1)``.

    Returns:
        The particle indices of each pair with ``shape=(n_pairs, 2)``, the squared
        distance between each pair with ``shape=(B, n_pairs)``, and the scale factor
        for each pair with ``shape=(n_pairs,)``.
    """

    n_particles = conformer.shape[-2]

    pair_idxs = torch.triu_indices(n_particles, n_particles, 1).T
    pair_scales = smee.utils.ones_like(len(pair_idxs), other=exclusion_scales)

    if len(exclusions) > 0:
        exclusions, _ = exclusions.sort(dim=1)

        i, j = exclusions[:, 0], exclusions[:, 1]
        exclusions_1d = ((i * (2 * n_particles - i - 1)) / 2 + j - i - 1).long()

        pair_scales[exclusions_1d] = exclusion_scales.squeeze(-1)

    directions = conformer[:, pair_idxs[:, 1], :] - conformer[:, pair_idxs[:, 0], :]
    distances_sqr = (directions * directions).sum(dim=-1)

    return pair_idxs, distances_sqr, pair_scales


@smee.potentials.potential_energy_fn("vdW", _LJ_POTENTIAL)
def compute_lj_energy(
    conformer: torch.Tensor,
    parameters: torch.Tensor,
    exclusions: torch.Tensor,
    exclusion_scales: torch.Tensor,
) -> torch.Tensor:
    """Evaluates the potential energy [kcal / mol] of the vdW interactions using the
    standard Lennard-Jones potential.

    Notes:
        * No cutoff will be applied.

    Args:
        conformer: The conformer [Å] to evaluate the potential at.
        parameters: A tensor containing the epsilon [kcal / mol] and sigma [Å] values
            of each particle, with ``shape=(n_particles, 2)``.
        exclusions: A tensor containing pairs of atom indices whose interaction should
            be scaled by ``exclusion_scales`` with ``shape=(n_exclusions, 2)``.
        exclusion_scales: A tensor containing the scale factor for each exclusion pair
            with ``shape=(n_exclusions, 1)``.

    Returns:
        The evaluated potential energy [kcal / mol].
    """

    is_batched = conformer.ndim == 3

    if not is_batched:
        conformer = torch.unsqueeze(conformer, 0)

    pair_idxs, distances_sqr, pair_scales = compute_pairwise(
        conformer, exclusions, exclusion_scales
    )

    epsilon, sigma = lorentz_berthelot(
        parameters[pair_idxs[:, 0], 0],
        parameters[pair_idxs[:, 1], 0],
        parameters[pair_idxs[:, 0], 1],
        parameters[pair_idxs[:, 1], 1],
    )

    x = sigma**6 / (distances_sqr**3)

    energy = (pair_scales * 4.0 * epsilon * (x * (x - 1.0))).sum(-1)

    if not is_batched:
        energy = torch.squeeze(energy, 0)

    return energy


def _compute_pme_exclusions(
    system: smee.TensorSystem, potential: smee.TensorPotential
) -> torch.Tensor:
    """Builds the exclusion tensor required by NNPOps pme functions

    Args:
        system: The system to compute the exclusions for.
        potential: The electrostatics potential.

    Returns:
        The exclusion tensor with ``shape=(n_particles, max_exclusions)`` where
        ``max_exclusions`` is the maximum number of exclusions of any atom. A value
        of -1 is used for padding.
    """
    exclusion_templates = [
        [[] for _ in range(topology.n_particles)] for topology in system.topologies
    ]
    max_exclusions = 0

    for exclusions, topology, n_copies in zip(
        exclusion_templates, system.topologies, system.n_copies
    ):
        for i, j in topology.parameters[potential.type].exclusions:
            exclusions[i].append(int(j))
            exclusions[j].append(int(i))

            max_exclusions = max(len(exclusions[i]), max_exclusions)
            max_exclusions = max(len(exclusions[j]), max_exclusions)

    idx_offset = 0

    exclusions_per_type = []

    for exclusions, topology, n_copies in zip(
        exclusion_templates, system.topologies, system.n_copies
    ):
        for atom_exclusions in exclusions:
            n_padding = max_exclusions - len(atom_exclusions)
            atom_exclusions.extend([-1] * n_padding)

        exclusion_offset = idx_offset + torch.arange(n_copies) * topology.n_particles

        exclusions = torch.broadcast_to(
            torch.tensor(exclusions, dtype=torch.int32),
            (n_copies, len(exclusions), max_exclusions),
        )
        exclusions = torch.where(
            exclusions >= 0, exclusions + exclusion_offset[:, None, None], exclusions
        )

        exclusions_per_type.append(exclusions.reshape(-1, max_exclusions))

        idx_offset += n_copies * topology.n_particles

    return torch.vstack(exclusions_per_type)


def _compute_pme_grid(
    box_vectors: torch.Tensor, cutoff: torch.Tensor, error_tolerance: torch.Tensor
) -> tuple[int, int, int, float]:
    alpha = torch.sqrt(-torch.log(2.0 * error_tolerance)) / cutoff

    factor = 2.0 * alpha / (3 * error_tolerance ** (1.0 / 5.0))

    grid_x = torch.maximum(torch.ceil(factor * box_vectors[0, 0]), _PME_MIN_NODES)
    grid_y = torch.maximum(torch.ceil(factor * box_vectors[1, 1]), _PME_MIN_NODES)
    grid_z = torch.maximum(torch.ceil(factor * box_vectors[2, 2]), _PME_MIN_NODES)

    return int(grid_x), int(grid_y), int(grid_z), float(alpha)


def _compute_coulomb_energy_periodic(
    system: smee.TensorSystem,
    conformer: torch.Tensor,
    box_vectors: torch.Tensor,
    potential: smee.TensorPotential,
) -> torch.Tensor:
    """Compute the potential energy [kcal / mol] of a periodic system due to
    Coulomb interactions using PME.

    Args:
        system: The system to compute the energy for.
        conformer: The conformer [Å] to evaluate the potential at with
            ``shape=(n_particles, 3)``.
        box_vectors: The box vectors [Å] of the system. with ``shape=(3, 3)``.
        potential: The Coulomb potential.

    Returns:
        The potential energy [kcal / mol].
    """
    import NNPOps.pme

    assert system.is_periodic, "the system must be periodic."

    charges = (
        smee.potentials.broadcast_parameters(system, potential).squeeze(-1).float()
    )

    cutoff = potential.attributes[potential.attribute_cols.index("cutoff")]
    error_tol = torch.tensor(0.0001)

    pairs, deltas, distances = _compute_pairwise(conformer, box_vectors, cutoff)
    exceptions = _compute_pme_exclusions(system, potential)

    grid_x, grid_y, grid_z, alpha = _compute_pme_grid(box_vectors, cutoff, error_tol)

    pme = NNPOps.pme.PME(
        grid_x, grid_y, grid_z, _PME_ORDER, alpha, _COULOMB_PRE_FACTOR, exceptions
    )

    energy_direct = torch.ops.pme.pme_direct(
        conformer.float(),
        charges,
        pairs,
        deltas,
        distances,
        pme.exclusions,
        pme.alpha,
        pme.coulomb,
    )
    energy_self = (
        -torch.sum(charges**2) * pme.coulomb * pme.alpha / math.sqrt(torch.pi)
    )
    energy_recip = energy_self + torch.ops.pme.pme_reciprocal(
        conformer.float(),
        charges,
        box_vectors.float(),
        pme.gridx,
        pme.gridy,
        pme.gridz,
        pme.order,
        pme.alpha,
        pme.coulomb,
        pme.moduli[0],
        pme.moduli[1],
        pme.moduli[2],
    )

    exclusion_idxs, exclusion_scales = broadcast_exclusions(system, potential)

    exclusion_distances = (
        conformer[exclusion_idxs[:, 0], :] - conformer[exclusion_idxs[:, 1], :]
    ).norm(dim=-1)

    energy_exclusion = (
        _COULOMB_PRE_FACTOR
        * exclusion_scales
        * charges[exclusion_idxs[:, 0]]
        * charges[exclusion_idxs[:, 1]]
        / exclusion_distances
    ).sum(-1)

    return energy_direct + energy_recip + energy_exclusion


@smee.potentials.potential_energy_fn("Electrostatics", _COULOMB_POTENTIAL)
def compute_coulomb_energy(
    conformer: torch.Tensor,
    parameters: torch.Tensor,
    exclusions: torch.Tensor,
    exclusion_scales: torch.Tensor,
) -> torch.Tensor:
    """Evaluates the potential energy [kcal / mol] of the electrostatic interactions
    using the standard Coulomb potential.

    Notes:
        * No cutoff will be applied.

    Args:
        conformer: The conformer [Å] to evaluate the potential at.
        parameters: A tensor containing the charge [e] of each particle, with
            ``shape=(n_particles, 1)``.
        exclusions: A tensor containing pairs of atom indices whose interaction should
            be scaled by ``exclusion_scales`` with ``shape=(n_exclusions, 2)``.
        exclusion_scales: A tensor containing the scale factor for each exclusion pair
            with ``shape=(n_exclusions, 1)``.

    Returns:
        The evaluated potential energy [kcal / mol].
    """

    is_batched = conformer.ndim == 3

    if not is_batched:
        conformer = torch.unsqueeze(conformer, 0)

    pair_idxs, distances_sqr, pair_scales = compute_pairwise(
        conformer, exclusions, exclusion_scales
    )
    inverse_distances = torch.rsqrt(distances_sqr)

    energy = (
        _COULOMB_PRE_FACTOR
        * pair_scales
        * parameters[pair_idxs[:, 0], 0]
        * parameters[pair_idxs[:, 1], 0]
        * inverse_distances
    ).sum(-1)

    if not is_batched:
        energy = torch.squeeze(energy, 0)

    return energy
