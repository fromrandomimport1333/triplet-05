/*
 * Umbrella header. Includes every module. C99, flat extern "C" ABI -- no
 * C++ types, no exceptions, no name mangling cross this boundary. Every
 * function takes plain numeric parameters (probabilities, weights,
 * likelihood ratios, costs, indices); nothing here is tied to any one
 * project's vocabulary.
 */
#ifndef CITYBRAIN_CITYBRAIN_H
#define CITYBRAIN_CITYBRAIN_H

#include "citybrain/common.h"
#include "citybrain/fusion.h"
#include "citybrain/detector.h"
#include "citybrain/graph.h"
#include "citybrain/decision.h"
#include "citybrain/assignment.h"
#include "citybrain/cache.h"
#include "citybrain/calibration.h"
#include "citybrain/mdp.h"
#include "citybrain/example_profile.h"
#include "citybrain/physics.h"

#endif /* CITYBRAIN_CITYBRAIN_H */
