import os
from custodian.vasp.jobs import VaspJob
from custodian.vasp.handlers import VaspErrorHandler, FrozenJobErrorHandler, MeshSymmetryErrorHandler
from fireworks.core.firework import FireWork, Workflow
from mpworks.dupefinders.dupefinder_vasp import DupeFinderVasp
from mpworks.firetasks.controller_tasks import AddEStructureTask
from mpworks.firetasks.custodian_task import VaspCustodianTask
from mpworks.firetasks.snl_tasks import AddSNLTask
from mpworks.firetasks.vasp_io_tasks import VaspCopyTask, VaspWriterTask, \
    VaspToDBTask
from mpworks.firetasks.vasp_setup_tasks import SetupGGAUTask, \
    SetupStaticRunTask, SetupNonSCFTask
from pymatgen import Composition
from pymatgen.io.cifio import CifParser
from pymatgen.io.vaspio_set import MPVaspInputSet, MPGGAVaspInputSet
from pymatgen.matproj.snl import StructureNL

__author__ = 'Anubhav Jain'
__copyright__ = 'Copyright 2013, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Mar 15, 2013'

# TODO: add duplicate checks for DB task - don't want to add the same dir
# twice!!
# TODO: different walltime requirements and priority for DB task


def _get_custodian_task(spec):
    task_type = spec['task_type']
    v_exe = 'VASP_EXE'  # will be transformed to vasp executable on the node
    if 'optimize structure (2x)' in task_type:
        jobs = VaspJob.double_relaxation_run(v_exe, gzipped=False)
    else:
        jobs = [VaspJob(v_exe)]

    handlers = [VaspErrorHandler(), FrozenJobErrorHandler(), MeshSymmetryErrorHandler()]
    params = {'jobs': [j.to_dict for j in jobs],
              'handlers': [h.to_dict for h in handlers], 'max_errors': 10, 'auto_npar': False, 'auto_gamma': False}

    return VaspCustodianTask(params)


def _snl_to_spec(snl, enforce_gga=True):
    spec = {}

    mpvis = MPGGAVaspInputSet() if enforce_gga else MPVaspInputSet()
    structure = snl.structure

    spec['vasp'] = {}
    spec['vasp']['incar'] = mpvis.get_incar(structure).to_dict
    spec['vasp']['incar']['NPAR'] = 2
    spec['vasp']['poscar'] = mpvis.get_poscar(structure).to_dict
    spec['vasp']['kpoints'] = mpvis.get_kpoints(structure).to_dict
    spec['vasp']['potcar'] = mpvis.get_potcar(structure).to_dict
    spec['_dupefinder'] = DupeFinderVasp().to_dict()
    spec['_priority'] = 2
    # TODO: restore category
    # spec['_category'] = 'Materials Project'
    spec['vaspinputset_name'] = mpvis.__class__.__name__
    spec['task_type'] = 'GGA+U optimize structure (2x)' if spec['vasp'][
        'incar'].get('LDAU', False) else 'GGA optimize structure (2x)'

    spec.update(_get_metadata(snl))

    return spec


def _get_metadata(snl):
    md = {'run_tags': ['auto generation v1.0']}
    if '_materialsproject' in snl.data and 'submission_id' in snl.data[
        '_materialsproject']:
        md['submission_id'] = snl.data['_materialsproject']['submission_id']

    return md


def snl_to_wf(snl, do_bandstructure=True):
    # TODO: clean this up once we're out of testing mode
    # TODO: add WF metadata
    fws = []
    connections = {}

    # add the SNL to the SNL DB and figure out duplicate group
    tasks = [AddSNLTask()]
    spec = {'task_type': 'Add to SNL database', 'snl': snl.to_dict}
    fws.append(FireWork(tasks, spec, name=spec['task_type'], fw_id=0))
    connections[0] = 1

    # run GGA structure optimization
    spec = _snl_to_spec(snl, enforce_gga=True)
    tasks = [VaspWriterTask(), _get_custodian_task(spec)]
    fws.append(FireWork(tasks, spec, name=spec['task_type'], fw_id=1))

    # insert into DB - GGA structure optimization
    spec = {'task_type': 'VASP db insertion', '_priority': 2,
            '_allow_fizzled_parents': True}
    spec.update(_get_metadata(snl))
    fws.append(FireWork([VaspToDBTask()], spec, name=spec['task_type'], fw_id=2))
    connections[1] = 2

    if do_bandstructure:
        spec = {'task_type': 'Controller: add Electronic Structure'}
        spec.update(_get_metadata(snl))
        fws.append(
            FireWork([AddEStructureTask()], spec, name=spec['task_type'], fw_id=3))
        connections[2] = 3

    # determine if GGA+U FW is needed
    incar = MPVaspInputSet().get_incar(snl.structure).to_dict

    if 'LDAU' in incar and incar['LDAU']:
        spec = {'task_type': 'GGA+U optimize structure (2x)',
                '_dupefinder': DupeFinderVasp().to_dict()}
        spec.update(_get_metadata(snl))
        fws.append(FireWork(
            [VaspCopyTask({'extension': '.relax2'}), SetupGGAUTask(),
             _get_custodian_task(spec)], spec, name=spec['task_type'], fw_id=10))
        connections[2].append(10)

        spec = {'task_type': 'VASP db insertion',
                '_allow_fizzled_parents': True}
        spec.update(_get_metadata(snl))
        fws.append(
            FireWork([VaspToDBTask()], spec, name=spec['task_type'], fw_id=11))
        connections[10] = 11

        if do_bandstructure:
            spec = {'task_type': 'Controller: add Electronic Structure'}
            spec.update(_get_metadata(snl))
            fws.append(FireWork([AddEStructureTask()], spec, name=spec['task_type'], fw_id=12))
            connections[11] = 12

    return Workflow(fws, connections, name=Composition.from_formula(snl.structure.composition.reduced_formula).alphabetical_formula)


def snl_to_wf_ggau(snl):

    # TODO: add WF meta

    fws = []
    connections = {}

    # add the root FW (GGA+U)
    spec = _snl_to_spec(snl, enforce_gga=False)
    tasks = [VaspWriterTask(), _get_custodian_task(spec)]
    fws.append(FireWork(tasks, spec, fw_id=1))

    # add GGA insertion to DB
    spec = {'task_type': 'VASP db insertion', '_priority': 2,
            '_category': 'VASP'}
    spec.update(_get_metadata(snl))
    fws.append(FireWork([VaspToDBTask()], spec, fw_id=2))
    connections[1] = 2
    mpvis = MPVaspInputSet()

    spec['vaspinputset_name'] = mpvis.__class__.__name__

    return Workflow(fws, connections, name=Composition.from_formula(snl.structure.composition.reduced_formula).alphabetical_formula)


if __name__ == '__main__':
    s1 = CifParser('test_wfs/Si.cif').get_structures()[0]
    s2 = CifParser('test_wfs/FeO.cif').get_structures()[0]

    snl1 = StructureNL(s1, "Anubhav Jain <ajain@lbl.gov>")
    snl2 = StructureNL(s2, "Anubhav Jain <ajain@lbl.gov>")

    snl_to_wf(snl1).to_file('test_wfs/wf_si_dupes.json', indent=4)
    snl_to_wf(snl2).to_file('test_wfs/wf_feo_dupes.json', indent=4)
