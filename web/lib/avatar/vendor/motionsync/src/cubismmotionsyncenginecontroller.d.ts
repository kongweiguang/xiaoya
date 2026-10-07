/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmString } from '@framework/type/csmstring';
import { csmVector } from '@framework/type/csmvector';
import { EngineType } from './cubismmotionsyncutil';
import { ICubismMotionSyncEngine } from './icubismmotionsyncengine';
export declare class CubismMotionSyncEngineController {
    static initializeEngine(engineConfig: number): ICubismMotionSyncEngine;
    static getEngine(type: EngineType): ICubismMotionSyncEngine;
    static getEngines(): csmVector<ICubismMotionSyncEngine>;
    static releaseEngineNotForce(engine: ICubismMotionSyncEngine): void;
    static releaseEngine(engine: ICubismMotionSyncEngine, isForce: boolean): void;
    static deleteAllEngine(): void;
    static ToEngineType(engineName: csmString): EngineType;
    static deleteAssociation(engine: ICubismMotionSyncEngine): void;
    private static _engineMap;
}
import * as $ from './cubismmotionsyncenginecontroller';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncEngineController: typeof $.CubismMotionSyncEngineController;
    type CubismMotionSyncEngineController = $.CubismMotionSyncEngineController;
}
